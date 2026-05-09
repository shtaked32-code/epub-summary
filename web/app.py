#!/usr/bin/env python3
"""
FastAPI веб-интерфейс для epub_summary.py.
Запуск: uvicorn app:app --reload  (из папки web/)
"""

import asyncio
import json
import shutil
import sys
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from epub_summary import run_analysis

OUTPUT_DIR = Path("/Users/seregafrolov/Documents/Claude/v0005")
BOOKS_DIR  = OUTPUT_DIR / "Book"
UPLOAD_DIR = OUTPUT_DIR / ".uploads"
BOOKS_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)


def _unwrap_safari_zip(epub_path: Path) -> None:
    """Safari при drag-and-drop упаковывает epub в zip, где содержимое лежит
    внутри папки BookName.epub/. Пересобираем правильный epub-архив."""
    import io
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names = zf.namelist()

            # Уже валидный epub — ничего делать не нужно
            if "META-INF/container.xml" in names:
                return

            # Safari-случай: BookName.epub/META-INF/container.xml
            epub_prefix = None
            for name in names:
                if name.endswith(".epub/META-INF/container.xml"):
                    epub_prefix = name[: -len("META-INF/container.xml")]
                    break

            if not epub_prefix:
                # Запасной вариант: внутри лежит один .epub-файл
                epub_members = [n for n in names if n.lower().endswith(".epub")]
                if epub_members:
                    epub_path.write_bytes(zf.read(epub_members[0]))
                return

            # Пересобираем epub: strip prefix, mimetype первым и без сжатия
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as out:
                mimetype_key = epub_prefix + "mimetype"
                if mimetype_key in names:
                    info = zipfile.ZipInfo("mimetype")
                    info.compress_type = zipfile.ZIP_STORED
                    out.writestr(info, zf.read(mimetype_key))
                for item in zf.infolist():
                    n = item.filename
                    if not n.startswith(epub_prefix):
                        continue
                    if "__MACOSX" in n:
                        continue
                    new_name = n[len(epub_prefix):]
                    if not new_name or new_name == "mimetype":
                        continue  # уже добавили / папка
                    out.writestr(new_name, zf.read(n))
            epub_path.write_bytes(buf.getvalue())

    except Exception as e:
        print(f"[DEBUG] _unwrap_safari_zip error: {e}", flush=True)


def _fix_epub_namespace(epub_path: Path) -> None:
    """Некоторые epub имеют неправильный namespace в container.xml.
    ebooklib требует urn:oasis:names:tc:opendocument:xmlns:container.
    Пересобираем zip с исправленным container.xml если нужно."""
    import io
    CORRECT_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
    try:
        with zipfile.ZipFile(epub_path) as zf:
            if "META-INF/container.xml" not in zf.namelist():
                return
            raw = zf.read("META-INF/container.xml")
            text = raw.decode("utf-8", errors="replace")
            if CORRECT_NS in text:
                return  # namespace уже правильный
            # Заменяем любой вариант xmlns= в теге container на правильный
            import re
            fixed = re.sub(
                r'(xmlns\s*=\s*")[^"]*(")',
                rf'\g<1>{CORRECT_NS}\g<2>',
                text,
            )
            if fixed == text:
                return  # ничего не поменялось
            fixed_bytes = fixed.encode("utf-8")
            # Пересобираем zip
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as out:
                for item in zf.infolist():
                    if item.filename == "META-INF/container.xml":
                        out.writestr(item.filename, fixed_bytes)
                    else:
                        out.writestr(item.filename, zf.read(item.filename))
            epub_path.write_bytes(buf.getvalue())
    except Exception as e:
        print(f"[DEBUG] _fix_epub_namespace error: {e}", flush=True)


def _extract_verdict(content: str) -> str:
    """Возвращает 'yes', 'no' или '' если вердикт не найден.
    Ищет ДА/НЕТ в первых нескольких строках секции — на случай если модель
    не начала ответ с нужного слова."""
    import re
    in_section = False
    lines_checked = 0
    for line in content.splitlines():
        s = line.strip()
        if "СТОИТ ЛИ ЧИТАТЬ" in s or "WORTH READING" in s:
            in_section = True
            continue
        if not in_section or s.startswith("="):
            continue
        if not s:
            continue
        lines_checked += 1
        u = s.upper()
        # Ищем ДА/НЕТ/YES/NO в любом месте строки (не только в начале)
        if re.search(r'\bДА\b', u) or u.startswith("YES") or re.search(r'\bYES\b', u):
            return "yes"
        if re.search(r'\bНЕТ\b', u) or u.startswith("NO") or re.search(r'\bNO\b', u):
            return "no"
        if lines_checked >= 3:
            break  # смотрим только первые 3 строки секции
    return ""

app = FastAPI(title="Epub Анализатор")
executor = ThreadPoolExecutor(max_workers=2)

# job_id -> список событий (буфер для клиентов, подключившихся позже)
_job_events: dict[str, list[dict]] = {}
# job_id -> Queue для живой раздачи SSE
_job_queues: dict[str, asyncio.Queue] = {}


def _push(job_id: str, event: dict) -> None:
    """Добавляет событие в буфер и в очередь SSE (если клиент подключён)."""
    _job_events.setdefault(job_id, []).append(event)
    if q := _job_queues.get(job_id):
        q.put_nowait(event)


# ─── Маршруты ─────────────────────────────────────────────────────────────────

def _html() -> HTMLResponse:
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))

@app.get("/", response_class=HTMLResponse)
async def index():
    return _html()

@app.get("/book", response_class=HTMLResponse)
async def index_book():
    return _html()


@app.post("/api/analyze")
async def analyze(
    file: UploadFile,
    lang: str = Form("ru"),
    detail: str = Form("short"),
):
    job_id = str(uuid.uuid4())
    _job_events[job_id] = []

    # Сохраняем загруженный файл
    epub_path = UPLOAD_DIR / f"{job_id}.epub"
    with epub_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Safari упаковывает epub в zip — распаковываем если нужно
    _unwrap_safari_zip(epub_path)
    # Исправляем неправильный namespace в container.xml если нужно
    _fix_epub_namespace(epub_path)

    original_stem = Path(file.filename).stem
    output_path = BOOKS_DIR / f"{original_stem}.txt"

    asyncio.create_task(_run_job(job_id, epub_path, output_path, lang, detail))
    return {"job_id": job_id}


async def _run_job(
    job_id: str,
    epub_path: Path,
    output_path: Path,
    lang: str,
    detail: str,
) -> None:
    loop = asyncio.get_running_loop()

    def on_progress(pct: int, msg: str, **extra):
        event = {"percent": pct, "message": msg, "status": "running", **extra}
        loop.call_soon_threadsafe(_push, job_id, event)

    def run():
        return run_analysis(epub_path, lang, detail, output_path, on_progress)

    try:
        result_path, metadata = await loop.run_in_executor(executor, run)
        event = {
            "percent": 100,
            "status": "done",
            "message": "Готово!" if lang == "ru" else "Done!",
            "filename": result_path.name,
            "title": metadata["title"],
            "author": metadata["author"],
        }
    except Exception as e:
        event = {"percent": 100, "status": "error", "message": str(e)}

    loop.call_soon_threadsafe(_push, job_id, event)
    epub_path.unlink(missing_ok=True)


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    queue: asyncio.Queue = asyncio.Queue()
    _job_queues[job_id] = queue

    # Отдаём события, накопившиеся до подключения клиента
    for ev in _job_events.get(job_id, []):
        await queue.put(ev)

    async def generate():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=600)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if ev.get("status") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"status":"error","message":"Timeout"}\n\n'
                    break
        finally:
            _job_queues.pop(job_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/books")
async def list_books():
    books = []
    for txt in sorted(
        BOOKS_DIR.glob("*.txt"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    ):
        content = txt.read_text(encoding="utf-8", errors="replace")
        title = author = "—"
        for line in content.splitlines():
            s = line.strip()
            if s.startswith("Название:") or s.startswith("Title:"):
                title = s.split(":", 1)[1].strip()
            elif s.startswith("Автор:") or s.startswith("Author:"):
                author = s.split(":", 1)[1].strip()
            if title != "—" and author != "—":
                break
        books.append({
            "filename": txt.name,
            "title": title,
            "author": author,
            "verdict": _extract_verdict(content),
            "mtime": txt.stat().st_mtime,
        })
    return books


@app.get("/api/books/{filename}")
async def get_book(filename: str):
    if any(c in filename for c in "/\\"):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    txt_path = (BOOKS_DIR / filename).resolve()
    if txt_path.parent != BOOKS_DIR:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not txt_path.exists() or txt_path.suffix != ".txt":
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"content": txt_path.read_text(encoding="utf-8")})
