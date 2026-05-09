#!/usr/bin/env python3
"""
epub_summary.py — анализирует epub-книгу и сохраняет:
  - краткое содержание книги
  - основные идеи книги
Использует локальную модель через ollama — без API-ключа и без затрат токенов.

Использование:
    python epub_summary.py <путь_к_файлу.epub> [параметры]

Параметры:
    --lang    ru|en          язык результата (по умолчанию: ru)
    --output  <папка>        куда сохранить результат (по умолчанию: папка исходного файла)
    --detail  short|full     краткий (2 предложения на главу) или полный (5 предложений)
"""

import sys
import re
import argparse
from pathlib import Path

try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    print("Ошибка: библиотека ebooklib не установлена.")
    print("  Установите: pip install ebooklib")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Ошибка: библиотека beautifulsoup4 не установлена.")
    print("  Установите: pip install beautifulsoup4")
    sys.exit(1)

try:
    import ollama
except ImportError:
    print("Ошибка: библиотека ollama не установлена.")
    print("  Установите: pip install ollama")
    sys.exit(1)

OLLAMA_MODEL = "qwen3:14b"

# ─── Локализация ──────────────────────────────────────────────────────────────

STRINGS = {
    "ru": {
        "processing":       "Обрабатываю: {name}",
        "checking_model":   "Проверяю модель {model}...",
        "model_ready":      "  Модель готова.",
        "reading":          "Читаю книгу...",
        "title":            "  Название: {title}",
        "author":           "  Автор:    {author}",
        "chapters_found":   "  Найдено глав: {n}",
        "writing_summary":  "Пишу краткое содержание...",
        "done_summary":     "  Готово ({n} символов).",
        "writing_ideas":    "Выделяю основные идеи...",
        "done_ideas":       "  Готово.",
        "writing_verdict":  "Выношу вердикт...",
        "done_verdict":     "  Готово.",
        "success":          "Готово. Результат: {path}",
        "err_not_found":    "Ошибка: файл не найден — «{path}»\n  Проверьте путь и попробуйте снова.",
        "err_not_epub":     "Ошибка: файл «{name}» не является epub-файлом.\n  Ожидалось расширение .epub, получено: «{ext}»",
        "err_ollama":       "Ошибка: не удалось подключиться к Ollama.\n  Убедитесь, что приложение Ollama запущено (ollama serve).",
        "err_no_model":     "Ошибка: модель «{model}» не найдена.\n  Установите её: ollama pull {model}\n  Доступные модели: {available}",
        "err_read_epub":    "Ошибка при чтении epub-файла: {err}\n  Файл может быть повреждён или иметь нестандартный формат.",
        "err_no_chapters":  "Ошибка: не удалось извлечь ни одной главы.\n  Файл может быть защищён DRM или иметь нестандартную структуру.",
        "err_output_dir":   "Ошибка: папка для результата не существует — «{path}»\n  Создайте её или укажите другой путь.",
        "header_analysis":  "АНАЛИЗ КНИГИ",
        "header_summary":   "КРАТКОЕ СОДЕРЖАНИЕ",
        "header_ideas":     "ОСНОВНЫЕ ИДЕИ",
        "header_verdict":   "СТОИТ ЛИ ЧИТАТЬ",
        "label_title":      "Название",
        "label_author":     "Автор",
        "label_chapters":   "Количество глав",
        "label_detail":     "Детализация",
        "detail_short":     "краткая",
        "detail_full":      "полная",
        "footer":           "Конец анализа",
    },
    "en": {
        "processing":       "Processing: {name}",
        "checking_model":   "Checking model {model}...",
        "model_ready":      "  Model ready.",
        "reading":          "Reading book...",
        "title":            "  Title:  {title}",
        "author":           "  Author: {author}",
        "chapters_found":   "  Chapters found: {n}",
        "writing_summary":  "Writing summary...",
        "done_summary":     "  Done ({n} characters).",
        "writing_ideas":    "Extracting key ideas...",
        "done_ideas":       "  Done.",
        "writing_verdict":  "Writing verdict...",
        "done_verdict":     "  Done.",
        "success":          "Done. Result saved to: {path}",
        "err_not_found":    "Error: file not found — \"{path}\"\n  Check the path and try again.",
        "err_not_epub":     "Error: file \"{name}\" is not an epub file.\n  Expected .epub extension, got: \"{ext}\"",
        "err_ollama":       "Error: could not connect to Ollama.\n  Make sure the Ollama app is running (ollama serve).",
        "err_no_model":     "Error: model \"{model}\" not found.\n  Install it: ollama pull {model}\n  Available models: {available}",
        "err_read_epub":    "Error reading epub file: {err}\n  The file may be corrupted or use a non-standard format.",
        "err_no_chapters":  "Error: no chapters could be extracted.\n  The file may be DRM-protected or have a non-standard structure.",
        "err_output_dir":   "Error: output folder does not exist — \"{path}\"\n  Create it or specify a different path.",
        "header_analysis":  "BOOK ANALYSIS",
        "header_summary":   "SUMMARY",
        "header_ideas":     "KEY IDEAS",
        "header_verdict":   "WORTH READING?",
        "label_title":      "Title",
        "label_author":     "Author",
        "label_chapters":   "Chapters",
        "label_detail":     "Detail level",
        "detail_short":     "short",
        "detail_full":      "full",
        "footer":           "End of analysis",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    return STRINGS[lang][key].format(**kwargs)


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Анализирует epub-книгу: краткое содержание и основные идеи.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python epub_summary.py book.epub\n"
            "  python epub_summary.py book.epub --lang en\n"
            "  python epub_summary.py book.epub --output ~/summaries --detail full\n"
        ),
    )
    parser.add_argument("epub_file", help="Путь к epub-файлу")
    parser.add_argument(
        "--lang",
        choices=["ru", "en"],
        default="ru",
        help="Язык результата: ru (по умолчанию) или en",
    )
    parser.add_argument(
        "--output",
        metavar="ПАПКА",
        default=None,
        help="Папка для сохранения результата (по умолчанию: папка исходного файла)",
    )
    parser.add_argument(
        "--detail",
        choices=["short", "full"],
        default="short",
        help="Детализация: short — 2 предложения на главу (по умолчанию), full — 5 предложений",
    )
    return parser.parse_args()


def validate_epub_path(path_str: str, lang: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        print(t(lang, "err_not_found", path=path))
        sys.exit(1)
    if path.suffix.lower() != ".epub":
        print(t(lang, "err_not_epub", name=path.name, ext=path.suffix))
        sys.exit(1)
    return path


def resolve_output_path(epub_path: Path, output_arg: str | None, lang: str) -> Path:
    if output_arg is None:
        return epub_path.with_suffix(".txt")
    output_dir = Path(output_arg).expanduser().resolve()
    if not output_dir.exists():
        print(t(lang, "err_output_dir", path=output_dir))
        sys.exit(1)
    return output_dir / epub_path.with_suffix(".txt").name


def check_ollama_model(lang: str):
    try:
        models = ollama.list()
        available = [m.model for m in models.models]
        if not any(OLLAMA_MODEL in m for m in available):
            print(t(lang, "err_no_model",
                    model=OLLAMA_MODEL,
                    available=", ".join(available) or "нет"))
            sys.exit(1)
    except Exception:
        print(t(lang, "err_ollama"))
        sys.exit(1)


def html_to_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_book_metadata(book: epub.EpubBook) -> dict:
    title = book.get_metadata("DC", "title")
    author = book.get_metadata("DC", "creator")
    return {
        "title": title[0][0] if title else "Неизвестно",
        "author": author[0][0] if author else "Неизвестно",
    }


def extract_chapters(book: epub.EpubBook) -> list[dict]:
    chapters = []
    for item_id, _ in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue

        raw_html = item.get_content().decode("utf-8", errors="replace")
        text = html_to_text(raw_html)

        if len(text) < 200:
            continue

        soup = BeautifulSoup(raw_html, "html.parser")
        heading = soup.find(re.compile(r"^h[1-3]$"))
        chapter_title = heading.get_text(strip=True) if heading else item.get_name()

        chapters.append({"title": chapter_title, "text": text})

    return chapters


def build_book_context(chapters: list[dict], detail: str) -> str:
    preview_len = 300 if detail == "short" else 800
    parts = []
    for i, ch in enumerate(chapters, 1):
        preview = ch["text"][:preview_len].replace("\n", " ")
        parts.append(f"Глава {i}. {ch['title']}: {preview}")
    return "\n".join(parts)


# ─── Работа с локальной моделью ───────────────────────────────────────────────

def ask_model(system: str, prompt: str) -> str:
    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        options={"temperature": 0.3},
        think=False,
    )
    return response.message.content.strip()


def generate_summary(context: str, metadata: dict, lang: str, detail: str) -> str:
    sentences_per_chapter = 2 if detail == "short" else 5
    length_hint = "1900–2100" if detail == "short" else "3500–4000"

    if lang == "ru":
        system = (
            "Ты литературный редактор. "
            "Пиши ТОЛЬКО на русском языке — даже если книга написана на другом языке. "
            "Переводи весь текст на русский. "
            "Давай ТОЛЬКО запрошенный текст — никаких вопросов, вариантов, предложений помочь, "
            "markdown-заголовков (#, ##), списков со звёздочками или дефисами."
        )
        prompt = (
            f"ЗАДАНИЕ: напиши краткое содержание книги объёмом {length_hint} символов.\n"
            f"На каждую главу — примерно {sentences_per_chapter} предложения.\n"
            f"Книга: «{metadata['title']}», автор: {metadata['author']}.\n\n"
            f"Фрагменты глав:\n{context}\n\n"
            f"Требования к оформлению:\n"
            f"- Раздели текст на логические абзацы (3–6 предложений в абзаце).\n"
            f"- Между абзацами — пустая строка.\n"
            f"- Каждый абзац начинается с новой мысли или этапа книги.\n"
            f"- Никаких заголовков, звёздочек, нумерации.\n\n"
            f"Напиши краткое содержание книги:"
        )
    else:
        system = (
            "You are a literary editor. "
            "Write ONLY in English. "
            "Provide ONLY the requested text — no questions, options, offers to help, "
            "markdown headers (#, ##), bullet points, or dashes."
        )
        prompt = (
            f"TASK: write a summary of the book of {length_hint} characters.\n"
            f"Cover each chapter in approximately {sentences_per_chapter} sentences.\n"
            f"Book: \"{metadata['title']}\", author: {metadata['author']}.\n\n"
            f"Chapter excerpts:\n{context}\n\n"
            f"Formatting requirements:\n"
            f"- Divide the text into logical paragraphs (3–6 sentences each).\n"
            f"- Leave a blank line between paragraphs.\n"
            f"- Each paragraph begins with a new idea or stage of the book.\n"
            f"- No headers, asterisks, or numbering.\n\n"
            f"Write the book summary:"
        )
    return ask_model(system, prompt)


def generate_key_ideas(context: str, metadata: dict, lang: str, detail: str) -> str:
    ideas_count = 7 if detail == "short" else 10

    if lang == "ru":
        system = (
            "Ты литературный редактор. "
            "Пиши ТОЛЬКО на русском языке — даже если книга написана на другом языке. "
            "Переводи все идеи и цитаты на русский. "
            "Давай ТОЛЬКО запрошенный текст — никаких вопросов, вариантов, предложений помочь, "
            "markdown-заголовков (#, ##) и дополнительных пояснений."
        )
        prompt = (
            f"ЗАДАНИЕ: выдели ровно {ideas_count} основных идей книги нумерованным списком.\n"
            f"Книга: «{metadata['title']}», автор: {metadata['author']}.\n\n"
            f"Фрагменты глав:\n{context}\n\n"
            f"Пиши ТОЛЬКО на русском языке.\n"
            f"{ideas_count} основных идей книги (нумерованный список, каждая идея 1–2 предложения):"
        )
    else:
        system = (
            "You are a literary editor. "
            "Write ONLY in English. "
            "Provide ONLY the requested text — no questions, options, offers to help, "
            "markdown headers (#, ##), or additional commentary."
        )
        prompt = (
            f"TASK: identify exactly {ideas_count} key ideas from the book as a numbered list.\n"
            f"Book: \"{metadata['title']}\", author: {metadata['author']}.\n\n"
            f"Chapter excerpts:\n{context}\n\n"
            f"{ideas_count} key ideas (numbered list, 1–2 sentences each):"
        )
    return ask_model(system, prompt)


def generate_verdict(context: str, metadata: dict, lang: str) -> str:
    if lang == "ru":
        system = (
            "Ты беспощадный литературный критик с высокими стандартами. "
            "Пиши ТОЛЬКО на русском языке — даже если книга написана на другом языке. "
            "Переводи все цитаты и идеи на русский. "
            "Давай ТОЛЬКО запрошенный текст — никаких вопросов, вариантов, предложений помочь, "
            "markdown-заголовков (#, ##), списков со звёздочками или дефисами. "
            "Большинство книг не заслуживают прочтения — они повторяют чужие идеи, "
            "переполнены водой и не дают ничего нового. Будь честен и беспощаден."
        )
        prompt = (
            f"ЗАДАНИЕ: вынеси жёсткий вердикт — стоит ли тратить время на эту книгу.\n"
            f"Книга: «{metadata['title']}», автор: {metadata['author']}.\n\n"
            f"Фрагменты глав:\n{context}\n\n"
            f"Пиши ТОЛЬКО на русском языке.\n\n"
            f"Критерии для «ДА» — книга должна соответствовать хотя бы двум из трёх:\n"
            f"1. Даёт оригинальные идеи, которых нет в других источниках.\n"
            f"2. Меняет способ мышления или даёт конкретные работающие инструменты.\n"
            f"3. Написана настолько хорошо, что сам стиль оправдывает чтение.\n\n"
            f"Если книга — пересказ известного, мотивационная вода или саморeklama автора — это «НЕТ».\n\n"
            f"Напиши ровно два абзаца:\n"
            f"1. Первый абзац начинается ПЕРВЫМ СЛОВОМ «ДА» или «НЕТ» — никакого текста до этого слова.\n"
            f"2. Второй абзац — конкретное объяснение слабых или сильных мест (2–3 предложения).\n"
            f"   ВАЖНО: если вердикт «НЕТ», второй абзац ОБЯЗАН заканчиваться фразой вида:\n"
            f"   «Вместо этого читай: «Название1» (Автор1), «Название2» (Автор2), «Название3» (Автор3).»\n"
            f"   Это должны быть РЕАЛЬНЫЕ существующие книги, напрямую связанные с темой данной книги. "
            f"   Фраза «более глубокие работы» без названий ЗАПРЕЩЕНА."
        )
    else:
        system = (
            "You are a ruthless literary critic with high standards. "
            "Write ONLY in English. "
            "Provide ONLY the requested text — no questions, options, offers to help, "
            "markdown headers (#, ##), bullet points, or dashes. "
            "Most books are not worth reading — they rehash existing ideas, are padded with filler, "
            "and offer nothing new. Be honest and unsparing."
        )
        prompt = (
            f"TASK: give a harsh verdict — is this book worth the reader's time?\n"
            f"Book: \"{metadata['title']}\", author: {metadata['author']}.\n\n"
            f"Chapter excerpts:\n{context}\n\n"
            f"Criteria for \"YES\" — the book must meet at least two of three:\n"
            f"1. Provides original ideas not found elsewhere.\n"
            f"2. Changes how you think or offers concrete, actionable tools.\n"
            f"3. Written so well that the style alone justifies reading.\n\n"
            f"If the book is a rehash of known ideas, motivational filler, or self-promotion — that's a \"NO\".\n\n"
            f"Write exactly two paragraphs:\n"
            f"1. The first paragraph starts with \"YES\" or \"NO\" and states the verdict (1–2 sentences).\n"
            f"2. The second paragraph gives a specific explanation of strengths or weaknesses (2–3 sentences).\n"
            f"   IMPORTANT: if the verdict is \"NO\", the second paragraph MUST end with a line like:\n"
            f"   \"Instead, read: 'Title1' (Author1), 'Title2' (Author2), 'Title3' (Author3).\"\n"
            f"   These must be REAL existing books directly related to this book's topic. "
            f"   Vague phrases like 'deeper works' without titles are FORBIDDEN."
        )
    return ask_model(system, prompt)


# ─── Основная функция анализа (для CLI и веб-интерфейса) ──────────────────────

def run_analysis(
    epub_path: Path,
    lang: str,
    detail: str,
    output_path: Path,
    on_progress=None,
) -> tuple[Path, dict]:
    """
    Анализирует epub и сохраняет результат в output_path.
    on_progress(percent, message, **extra) — опциональный колбэк прогресса.
    Возвращает (output_path, metadata).
    """
    def progress(pct: int, msg: str, **extra):
        if on_progress:
            on_progress(pct, msg, **extra)

    progress(5, t(lang, "checking_model", model=OLLAMA_MODEL))
    check_ollama_model(lang)

    progress(15, t(lang, "reading"))
    try:
        book = epub.read_epub(str(epub_path))
    except Exception as e:
        raise RuntimeError(t(lang, "err_read_epub", err=e))

    metadata = extract_book_metadata(book)
    chapters = extract_chapters(book)
    if not chapters:
        raise RuntimeError(t(lang, "err_no_chapters"))

    # Передаём название и автора вместе с прогрессом — веб-интерфейс покажет их
    progress(20, t(lang, "chapters_found", n=len(chapters)),
             title=metadata["title"], author=metadata["author"], chapters=len(chapters))

    context = build_book_context(chapters, detail)

    progress(30, t(lang, "writing_summary"))
    summary = generate_summary(context, metadata, lang, detail)

    progress(60, t(lang, "writing_ideas"))
    key_ideas = generate_key_ideas(context, metadata, lang, detail)

    progress(80, t(lang, "writing_verdict"))
    verdict = generate_verdict(context, metadata, lang)

    progress(93, "Сохраняю..." if lang == "ru" else "Saving...")

    detail_label = t(lang, "detail_short") if detail == "short" else t(lang, "detail_full")
    sep = "=" * 60
    lines = [
        sep,
        t(lang, "header_analysis"),
        sep,
        "",
        f"{t(lang, 'label_title')}:        {metadata['title']}",
        f"{t(lang, 'label_author')}:           {metadata['author']}",
        f"{t(lang, 'label_chapters')}: {len(chapters)}",
        f"{t(lang, 'label_detail')}:    {detail_label}",
        "",
        sep,
        t(lang, "header_summary"),
        sep,
        "",
        summary,
        "",
        sep,
        t(lang, "header_ideas"),
        sep,
        "",
        key_ideas,
        "",
        sep,
        t(lang, "header_verdict"),
        sep,
        "",
        verdict,
        "",
        sep,
        t(lang, "footer"),
        sep,
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")

    # Выгружаем модель из памяти после завершения — освобождаем GPU/CPU.
    progress(98, "Выгружаю модель..." if lang == "ru" else "Unloading model...")
    try:
        ollama.generate(model=OLLAMA_MODEL, prompt="", keep_alive=0)
    except Exception:
        pass

    return output_path, metadata


# ─── Точка входа CLI ──────────────────────────────────────────────────────────

def main():
    args = parse_args()
    lang = args.lang
    detail = args.detail

    epub_path = validate_epub_path(args.epub_file, lang)
    output_path = resolve_output_path(epub_path, args.output, lang)

    print(t(lang, "processing", name=epub_path.name))
    print()

    def on_progress(pct: int, msg: str, **extra):
        print(msg)
        if "title" in extra:
            print(t(lang, "title", title=extra["title"]))
            print(t(lang, "author", author=extra["author"]))

    try:
        output_path, _ = run_analysis(epub_path, lang, detail, output_path, on_progress)
    except RuntimeError as e:
        print(str(e))
        sys.exit(1)

    print()
    print(t(lang, "success", path=output_path))

    try:
        ollama.generate(model=OLLAMA_MODEL, prompt="", keep_alive=0)
    except Exception:
        pass


if __name__ == "__main__":
    main()
