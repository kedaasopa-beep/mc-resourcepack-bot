# -*- coding: utf-8 -*-
"""
Telegram-бот для автоматической публикации ресурспаков Minecraft.

Как это работает:
  1. Модератор отправляет боту .zip ресурспака.
  2. Бот скачивает архив во временную папку и распаковывает его.
  3. Читает pack.mcmeta и достаёт pack_format (версию пака).
  4. Отбирает «витрину» текстур: узнаваемые категории (блоки, шерсть,
     оружие/инструменты, броня), отсеивая служебный мусор и анимации.
  5. Собирает коллаж-превью 4x4 (ячейки 64x64, тёмный фон, отступы 8px).
     Блоки рисуются объёмными изометрическими кубами, предметы — спрайтами.
  6. Публикует в канал: сначала фото-превью с подписью, затем сам .zip.
  7. Отправляет модератору подтверждение "✅ Опубликовано".

Запуск на Bothost: переменные окружения BOT_TOKEN и CHANNEL_ID задаются в панели.
"""

import os
import json
import tempfile
import zipfile
import asyncio
import logging
from collections import Counter
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from PIL import Image

# --- Настройки из переменных окружения -------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")           # токен бота
CHANNEL_ID = os.getenv("CHANNEL_ID")         # ID канала (например, -1001234567890)

# Параметры превью
GRID = 4                 # сетка 4x4
CELL = 64                # размер одной ячейки
PAD = 8                  # отступы (внешние и между ячейками)
MAX_TEXTURES = 16        # максимум текстур в коллаже (4x4)
BG_COLOR = (32, 32, 32, 255)  # тёмный фон коллажа

# Объёмный рендер блоков (изометрический куб) вместо плоских текстур
RENDER_3D_BLOCKS = True
TOP_SHADE = 1.0          # яркость верхней грани
RIGHT_SHADE = 0.82       # яркость правой грани
LEFT_SHADE = 0.62        # яркость левой грани

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("resourcepack-bot")

# --- Работа с архивом --------------------------------------------------------

def safe_extract(zip_path: Path, dest: Path) -> None:
    """Распаковка zip с защитой от zip-slip (выход за пределы папки назначения)."""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest)):
                raise ValueError("Архив содержит недопустимые пути (zip-slip).")
        zf.extractall(dest)

def find_pack_mcmeta(root: Path) -> Path | None:
    """Находит pack.mcmeta в архиве (в том числе во вложенной папке)."""
    candidates = list(root.rglob("pack.mcmeta"))
    if not candidates:
        return None
    # Предпочитаем файл, лежащий ближе к корню
    candidates.sort(key=lambda p: len(p.relative_to(root).parts))
    return candidates[0]

def read_pack_format(mcmeta: Path) -> int | None:
    """Читает pack_format из pack.mcmeta. Возвращает None, если поля нет."""
    with mcmeta.open("r", encoding="utf-8") as f:
        data = json.load(f)
    pack_section = data.get("pack", {})
    return pack_section.get("pack_format")

def find_texture_files(root: Path) -> list[Path]:
    """
    Находит PNG-текстуры независимо от структуры пака.

    Сначала собирает всё под любой папкой `textures` (регистр не важен,
    работает и для Java, и для Bedrock). Если такой папки нет вовсе —
    берёт любые PNG, кроме иконки пака.
    """
    pngs: list[Path] = []
    for directory in root.rglob("*"):
        if directory.is_dir() and directory.name.lower() == "textures":
            pngs.extend(p for p in directory.rglob("*.png") if p.is_file())

    if not pngs:
        pngs = [
            p for p in root.rglob("*.png")
            if p.is_file() and p.name.lower() not in ("pack.png", "icon.png")
        ]
    return pngs

# --- Умный отбор текстур для «витрины» превью --------------------------------
#
# Мы не сватаем в коллаж всё подряд: показываем узнаваемые категории
# (блоки, шерсть, оружие/инструменты, броня) и отсеиваем служебный мусор.

# Слова в имени файла, которые означают «не показывать в превью»
JUNK_MARKERS = (
    "destroy_stage", "particle", "debug", "blur", "spawn_egg",
    "_overlay", "gui", "font", "logo", "paint",
    "water_flow", "water_still", "lava_flow", "lava_still",
    "fire_", "campfire_fire", "soul_fire",
)

# Легаси-имена (Minecraft 1.8–1.12) для плавного фолбэка: modern -> legacy
_WOOL_COLORS = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink",
    "gray", "light_gray", "cyan", "purple", "blue", "brown", "green",
    "red", "black",
)
ALIASES: dict[str, tuple[str, ...]] = {
    # шерсть: white_wool -> wool_colored_white
    **{f"{color}_wool": (f"wool_colored_{color}",) for color in _WOOL_COLORS},
    # броня 1.8.9 лежит единым листом models/armor/<материал>_layer_1|2.png
    "diamond_chestplate": ("diamond_layer_1",),
    "diamond_leggings": ("diamond_layer_1",),
    "diamond_helmet": ("diamond_layer_2",),
    "diamond_boots": ("diamond_layer_2",),
    "iron_chestplate": ("iron_layer_1",),
    "iron_leggings": ("iron_layer_1",),
    "iron_helmet": ("iron_layer_2",),
    "iron_boots": ("iron_layer_2",),
    "golden_chestplate": ("gold_layer_1",),
    "golden_leggings": ("gold_layer_1",),
    "golden_helmet": ("gold_layer_2",),
    "golden_boots": ("gold_layer_2",),
}

# Курированные группы: (название, сколько взять, приоритетные имена).
# Порядок групп = порядок рядов в коллаже: блоки / шерсть / оружие / броня.
GROUPS = (
    ("blocks", 4, (
        "ender_chest", "crafting_table_front", "obsidian", "tnt_side",
        "diamond_block", "anvil", "beacon", "bookshelf", "chest",
    )),
    ("wool", 4, (
        "white_wool", "red_wool", "blue_wool", "lime_wool",
        "orange_wool", "purple_wool",
    )),
    ("tools", 4, (
        "diamond_sword", "netherite_sword", "diamond_pickaxe", "diamond_axe",
        "golden_shovel", "bow", "crossbow", "shield", "trident",
    )),
    ("armor", 4, (
        "diamond_chestplate", "diamond_helmet", "iron_chestplate", "golden_chestplate",
        "netherite_chestplate", "iron_leggings", "golden_boots", "turtle_helmet",
    )),
)

def is_junk(png: Path) -> bool:
    """Отсеивает служебные и анимированные текстуры (particle, gui, fire и т.п.)."""
    stem = png.stem.lower()
    if any(marker in stem for marker in JUNK_MARKERS):
        return True
    if stem.endswith("_spawn_egg"):
        return True
    # Анимированные текстуры сопровождаются файлом .mcmeta — они «шумят» на превью
    if png.with_suffix(".png.mcmeta").exists():
        return True
    return False

def build_index(png_files: list[Path], *dirs: Path | None) -> dict[str, Path]:
    """Индексирует PNG по имени (без расширения). Папки `dirs` — приоритетные."""
    index: dict[str, Path] = {}
    for directory in dirs:
        if not directory:
            continue
        for png in sorted(directory.rglob("*.png")):
            if png.is_file():
                index.setdefault(png.stem.lower(), png)
    for png in sorted(png_files):
        index.setdefault(png.stem.lower(), png)
    return index

def lookup(index: dict[str, Path], name: str) -> Path | None:
    """Ищет текстуру по имени с учётом легаси-алиасов (шерсть, броня и т.п.)."""
    if name in index:
        return index[name]
    for alias in ALIASES.get(name, ()):
        if alias in index:
            return index[alias]
    return None

def select_textures(
    texture_files: list[Path], limit: int
) -> list[Path]:
    """
    Подбирает до `limit` текстур для превью.

    Сначала берёт курированные категории (блоки, шерсть, оружие, броня),
    затем, если слотов не хватило (кастомный/неполный пак), добирает
    оставшиеся непустые текстуры (папка block — в приоритете).
    """
    index = build_index(texture_files)
    selected: list[Path] = []
    seen: set[Path] = set()

    def add(png: Path | None) -> None:
        if png and png not in seen and not is_junk(png):
            seen.add(png)
            selected.append(png)

    # 1. Курированные группы в заданном порядке
    for _, cap, names in GROUPS:
        taken = 0
        for name in names:
            if taken >= cap or len(selected) >= limit:
                break
            png = lookup(index, name)
            if png and png not in seen and not is_junk(png):
                add(png)
                taken += 1

    # 2. Добор из общего пула (block приоритетнее item, порядок стабильный)
    if len(selected) < limit:
        pool = [p for p in index.values() if not is_junk(p)]
        pool.sort(key=lambda p: ("block" not in [part.lower() for part in p.parts], str(p).lower()))
        for png in pool:
            if len(selected) >= limit:
                break
            add(png)

    return selected[:limit]

# --- Построение коллажа ------------------------------------------------------

def is_block_texture(png: Path) -> bool:
    """Определяет, что текстура лежит в блоковой папке (block/blocks)."""
    parts = [part.lower() for part in png.parts]
    return "block" in parts or "blocks" in parts

def _shade(img: Image.Image, factor: float) -> Image.Image:
    """Затемняет RGB-каналы с сохранением прозрачности (имитация освещения)."""
    if factor >= 0.999:
        return img
    r, g, b, a = img.split()
    r = r.point(lambda v: int(v * factor))
    g = g.point(lambda v: int(v * factor))
    b = b.point(lambda v: int(v * factor))
    return Image.merge("RGBA", (r, g, b, a))

def render_block_3d(texture: Image.Image, cell: int) -> Image.Image:
    """
    Рисует объёмный изометрический куб из одной текстуры.

    Верхняя грань — ромб (2:1), боковые грани — параллелограммы,
    затемнённые по-разному, чтобы читался объём. Всё через аффинные
    преобразования с Image.NEAREST, чтобы пиксель-арт не мылился.
    """
    s = max(2, cell // 2)      # сторона грани в пикселях
    size = (2 * s, 2 * s)      # куб вписан в квадрат cell x cell
    src = texture.convert("RGBA").resize((s, s), Image.NEAREST)

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    # Верхняя грань: квадрат -> ромб (ширина 2s, высота s)
    top = src.transform(
        (2 * s, s),
        Image.AFFINE,
        (0.5, 1.0, -s / 2, -0.5, 1.0, s / 2),
        resample=Image.NEAREST,
        fillcolor=(0, 0, 0, 0),
    )
    canvas.alpha_composite(_shade(top, TOP_SHADE), (0, 0))

    # Правая грань (справа от ближнего угла)
    right = src.transform(
        size,
        Image.AFFINE,
        (-1.0, 0.0, 2 * s, 0.5, 1.0, -1.5 * s),
        resample=Image.NEAREST,
        fillcolor=(0, 0, 0, 0),
    )
    canvas.alpha_composite(_shade(right, RIGHT_SHADE), (0, 0))

    # Левая грань (слева от ближнего угла)
    left = src.transform(
        (s, 2 * s),
        Image.AFFINE,
        (1.0, 0.0, 0.0, -0.5, 1.0, -s / 2),
        resample=Image.NEAREST,
        fillcolor=(0, 0, 0, 0),
    )
    canvas.alpha_composite(_shade(left, LEFT_SHADE), (0, 0))

    return canvas

def render_texture(png: Path, cell: int) -> Image.Image:
    """Готовит одну текстуру для ячейки: блоки — кубом, предметы — спрайтом."""
    with Image.open(png) as raw:
        tex = raw.convert("RGBA")

    if RENDER_3D_BLOCKS and is_block_texture(png):
        return render_block_3d(tex, cell)

    # Плоские спрайты (предметы, броня) — вписываем с сохранением пропорций
    ratio = min(cell / tex.width, cell / tex.height)
    new_size = (max(1, round(tex.width * ratio)), max(1, round(tex.height * ratio)))
    if new_size != tex.size:
        tex = tex.resize(new_size, Image.NEAREST)
    return tex

def build_collage(png_paths: list[Path]) -> Image.Image:
    """Собирает коллаж из PNG-текстур с сохранением прозрачности и пропорций."""
    # Итоговый размер: (cols + 1) отступов + cols ячеек
    total = (GRID + 1) * PAD + GRID * CELL
    collage = Image.new("RGBA", (total, total), BG_COLOR)

    for index, png in enumerate(png_paths):
        col = index % GRID
        row = index // GRID
        x = PAD + col * (CELL + PAD)
        y = PAD + row * (CELL + PAD)

        tex = render_texture(png, CELL)

        # Центрируем внутри ячейки и вставляем с учётом альфа-канала
        offset_x = x + (CELL - tex.width) // 2
        offset_y = y + (CELL - tex.height) // 2
        collage.alpha_composite(tex, (offset_x, offset_y))

    return collage

def detect_resolution(png_paths: list[Path]) -> tuple[int, int]:
    """Определяет типичное разрешение текстур (самый частый размер)."""
    sizes = []
    for png in png_paths:
        with Image.open(png) as img:
            sizes.append(img.size)
    if not sizes:
        return (0, 0)
    return Counter(sizes).most_common(1)[0][0]

# --- Текст подписи -----------------------------------------------------------

def escape_md(text: str) -> str:
    """Экранирует символы, значимые для MarkdownV1."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

def build_caption(pack_name: str, pack_format, resolution: tuple[int, int]) -> str:
    """Формирует подпись под фото в Markdown."""
    fmt = str(pack_format) if pack_format is not None else "не указан"
    width, height = resolution
    return (
        f"*{escape_md(pack_name)}*\n"
        f"Версия: `pack_format {fmt}`\n"
        f"Разрешение: `{width}x{height}`\n"
        f"\n#resourcepack"
    )

# --- Хэндлеры -----------------------------------------------------------------

dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветствие для модератора."""
    await message.answer(
        "Отправь мне .zip файл ресурспака — я соберу превью и опубликую его в канал."
    )

@dp.message(F.document)
async def handle_document(message: Message, bot: Bot) -> None:
    """Основная логика: приём zip, обработка и публикация."""
    document = message.document
    file_name = document.file_name or "resourcepack.zip"

    # Принимаем только zip
    if not file_name.lower().endswith(".zip"):
        await message.answer("❌ Нужен файл формата .zip (ресурспак Minecraft).")
        return

    # Обрабатываем во временной папке, которая удалится автоматически
    with tempfile.TemporaryDirectory(prefix="rp_") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / file_name
        extract_dir = tmp_path / "unpacked"
        extract_dir.mkdir()

        # 1. Скачивание файла
        size_mb = (document.file_size or 0) / (1024 * 1024)
        logger.info("Получен файл %s (%.1f МБ)", file_name, size_mb)
        status = await message.answer(f"⏳ Скачиваю архив ({size_mb:.1f} МБ)...")
        try:
            # Таймаут, чтобы бот не «висел» бесконечно при проблемах с сетью
            await asyncio.wait_for(
                bot.download(document, destination=zip_path), timeout=120
            )
        except asyncio.TimeoutError:
            logger.exception("Таймаут скачивания")
            await status.edit_text("❌ Скачивание заняло слишком долго. Попробуй ещё раз.")
            return
        except Exception:
            logger.exception("Ошибка скачивания")
            await status.edit_text("❌ Не удалось скачать файл. Попробуй ещё раз.")
            return
        logger.info("Файл скачан: %s (%d байт)", zip_path.name, zip_path.stat().st_size)

        # 2. Распаковка
        try:
            safe_extract(zip_path, extract_dir)
        except zipfile.BadZipFile:
            await status.edit_text("❌ Архив повреждён или это не ZIP-файл.")
            return
        except Exception:
            logger.exception("Ошибка распаковки")
            await status.edit_text("❌ Не удалось распаковать архив.")
            return

        file_count = sum(1 for p in extract_dir.rglob("*") if p.is_file())
        logger.info("Архив распакован: %d файлов", file_count)

        # 3. Чтение pack.mcmeta
        mcmeta = find_pack_mcmeta(extract_dir)
        if mcmeta is None:
            await status.edit_text(
                "❌ В архиве не найден `pack.mcmeta`. Убедись, что это валидный ресурспак."
            )
            return
        try:
            pack_format = read_pack_format(mcmeta)
        except Exception:
            logger.exception("Ошибка чтения pack.mcmeta")
            await status.edit_text("❌ Не удалось прочитать pack.mcmeta (некорректный JSON).")
            return
        logger.info("pack.mcmeta: %s, pack_format=%s", mcmeta, pack_format)

        # 4. Поиск текстур: витрина из узнаваемых категорий (блоки/шерсть/оружие/броня)
        await status.edit_text("🖼 Собираю превью...")
        texture_files = find_texture_files(extract_dir)
        logger.info("Найдено PNG-текстур всего: %d", len(texture_files))

        selected = select_textures(texture_files, MAX_TEXTURES)
        logger.info("Отобрано для коллажа: %d", len(selected))
        for png in selected:
            logger.info("  %s", png.relative_to(extract_dir))

        if not selected:
            await status.edit_text(
                "❌ В архиве не найдено подходящих PNG-текстур "
                "(папка textures отсутствует или пуста)."
            )
            return

        # 5. Разрешение и коллаж
        try:
            resolution = detect_resolution(selected)
            collage = build_collage(selected)
        except Exception:
            logger.exception("Ошибка сборки коллажа")
            await status.edit_text("❌ Не удалось обработать PNG-текстуры.")
            return

        preview_path = tmp_path / "preview.png"
        # Убираем альфа-канал перед отправкой фото (Telegram не любит RGBA в JPEG/фото)
        collage.convert("RGB").save(preview_path, format="PNG")

        # 6. Публикация в канал
        pack_name = Path(file_name).stem
        caption = build_caption(pack_name, pack_format, resolution)

        try:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=FSInputFile(preview_path),
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
            await bot.send_document(
                chat_id=CHANNEL_ID,
                document=FSInputFile(zip_path, filename=file_name),
            )
        except Exception:
            logger.exception("Ошибка публикации в канал")
            await status.edit_text(
                "❌ Не удалось опубликовать в канал. Проверь, что бот — админ канала "
                "и CHANNEL_ID указан верно."
            )
            return

        # 7. Подтверждение модератору
        await status.edit_text(
            f"✅ Опубликовано\n"
            f"Текстур в превью: {len(selected)}\n"
            f"pack_format: {pack_format if pack_format is not None else 'не указан'}"
        )

# --- Точка входа --------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
    if not CHANNEL_ID:
        raise RuntimeError("Не задана переменная окружения CHANNEL_ID")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        # Пропускаем накопившиеся апдейты, чтобы не обрабатывать старые файлы
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
