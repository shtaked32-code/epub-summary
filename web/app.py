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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from epub_summary import run_analysis

OUTPUT_DIR = Path("/Users/seregafrolov/Documents/Claude/v0005")
UPLOAD_DIR = OUTPUT_DIR / ".uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

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

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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

    original_stem = Path(file.filename).stem
    output_path = OUTPUT_DIR / f"{original_stem}.txt"

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
        OUTPUT_DIR.glob("*.txt"),
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
            "mtime": txt.stat().st_mtime,
        })
    return books


@app.get("/api/books/{filename}")
async def get_book(filename: str):
    if any(c in filename for c in "/\\"):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    txt_path = (OUTPUT_DIR / filename).resolve()
    # Защита от path traversal
    if OUTPUT_DIR not in txt_path.parents and txt_path.parent != OUTPUT_DIR:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not txt_path.exists() or txt_path.suffix != ".txt":
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"content": txt_path.read_text(encoding="utf-8")})
