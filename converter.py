"""图片转 PDF 核心逻辑。

基于开源项目 fpdf2 (https://github.com/py-pdf/fpdf2) 与 Pillow 实现：
- 图片原始字节直接嵌入 PDF（JPEG 不重新编码），画质零损失
- EXIF 方向（手机照片旋转标记）通过 PDF 变换矩阵在显示层校正，不重编码像素
- PNG 等格式以无损压缩嵌入
- 支持纸张大小、页面方向、边距、图片是否适配页面等设置（适配只缩小不放大）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image
from fpdf import FPDF
from fpdf.drawing_primitives import Transform

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

# 显示名 -> FPDF 纸张参数；None 表示按图片尺寸生成纸张（跟随图片）
PAGE_SIZES: dict[str, str | None] = {
    "A3": "A3",
    "A4": "A4",
    "A5": "A5",
    "A6": "A6",
    "Letter": "Letter",
    "跟随图片": None,
}

ORIENTATION_PORTRAIT = "portrait"
ORIENTATION_LANDSCAPE = "landscape"
ORIENTATION_AUTO = "auto"
ORIENTATIONS = (ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE, ORIENTATION_AUTO)

_DEFAULT_DPI = 96.0
_EXIF_ORIENTATION_TAG = 0x0112


@dataclass(frozen=True)
class ConvertSettings:
    """一次转换的全部设置。"""

    page_size: str = "A4"  # PAGE_SIZES 的键
    orientation: str = ORIENTATION_PORTRAIT
    margin_mm: float = 0.0
    fit_to_page: bool = True


@dataclass
class _Probe:
    """单张图片的探测结果（展示尺寸已按 EXIF 方向换算）。"""

    width_px: int  # 展示尺寸（EXIF 校正后）
    height_px: int
    src_width_px: int  # 文件内原始存储尺寸
    src_height_px: int
    dpi: tuple[float, float]
    orientation: int  # EXIF 方向 1-8（非法值归为 1）
    source: str


def _read_dpi(im: Image.Image) -> tuple[float, float]:
    dpi = im.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and len(dpi) >= 2:
        try:
            dx, dy = float(dpi[0]), float(dpi[1])
        except (TypeError, ValueError):
            dx = dy = 0.0
        if dx > 0 and dy > 0:
            return dx, dy
    return _DEFAULT_DPI, _DEFAULT_DPI


def _probe(path: Path) -> _Probe:
    with Image.open(path) as im:
        dpi = _read_dpi(im)
        width, height = im.size
        try:
            orientation = int(im.getexif().get(_EXIF_ORIENTATION_TAG, 1))
        except (TypeError, ValueError):
            orientation = 1
    if orientation not in (2, 3, 4, 5, 6, 7, 8):
        orientation = 1
    # 方向 5-8 为转置类操作，展示时宽高互换
    src_width, src_height = width, height
    if orientation >= 5:
        width, height = height, width
    return _Probe(width, height, src_width, src_height, dpi, orientation, str(path))


def _orientation_transform(
    orientation: int, x: float, y: float, w: float, h: float, scale: float
) -> Transform:
    """构造将原始像素矩形（画在原点）映射到展示矩形 (x, y, w, h) 的变换。

    矩阵按 fpdf 的行向量约定：x' = a·x + c·y + e，y' = b·x + d·y + f，
    坐标系为 FPDF 用户坐标（左上原点，y 向下）。各方向的线性部分与
    平移量均按"绕原点旋转/翻转后平移入展示框"推导。
    """
    s = scale
    right, bottom = x + w, y + h
    table = {
        2: (-s, 0, 0, s, right, y),  # 水平翻转
        3: (-s, 0, 0, -s, right, bottom),  # 旋转 180°
        4: (s, 0, 0, -s, x, bottom),  # 垂直翻转
        5: (0, s, s, 0, x, y),  # 主对角线转置
        6: (0, s, -s, 0, right, y),  # 顺时针 90°
        7: (0, -s, -s, 0, right, bottom),  # 副对角线转置
        8: (0, -s, s, 0, x, bottom),  # 逆时针 90°
    }
    a, b, c, d, e, f = table[orientation]
    return Transform(a, b, c, d, e, f)


def convert_images(
    paths: Sequence[str | Path],
    output: str | Path,
    settings: ConvertSettings,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把多张图片按 settings 合成为一个 PDF，返回输出路径。

    progress(current, total) 每完成一张图片回调一次。
    """
    if not paths:
        raise ValueError("没有可转换的图片")
    if settings.page_size not in PAGE_SIZES:
        raise ValueError(f"未知页面大小: {settings.page_size}")
    if settings.orientation not in ORIENTATIONS:
        raise ValueError(f"未知页面方向: {settings.orientation}")
    margin = float(settings.margin_mm)
    if margin < 0:
        raise ValueError("边距不能为负数")

    pdf = FPDF(unit="mm")
    pdf.set_margins(margin, margin, margin)
    pdf.set_auto_page_break(auto=False, margin=margin)

    total = len(paths)
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(f"图片不存在: {path}")
        probe = _probe(path)
        natural_w = probe.width_px / probe.dpi[0] * 25.4
        natural_h = probe.height_px / probe.dpi[1] * 25.4

        paper = PAGE_SIZES[settings.page_size]
        if paper is None:
            # 跟随图片：纸张 = 图片原始尺寸 + 边距
            page_w = natural_w + 2 * margin
            page_h = natural_h + 2 * margin
            pdf.add_page(format=(page_w, page_h), orientation=ORIENTATION_PORTRAIT)
        else:
            orientation = settings.orientation
            if orientation == ORIENTATION_AUTO:
                orientation = (
                    ORIENTATION_LANDSCAPE if natural_w > natural_h else ORIENTATION_PORTRAIT
                )
            pdf.add_page(format=paper, orientation=orientation)

        avail_w, avail_h = pdf.epw, pdf.eph
        if settings.fit_to_page:
            # 只缩小不放大：保持原生像素密度，避免放大导致显示模糊
            scale = min(1.0, avail_w / natural_w, avail_h / natural_h)
            disp_w, disp_h = natural_w * scale, natural_h * scale
        else:
            scale = 1.0
            disp_w, disp_h = natural_w, natural_h

        # 在可用区域内居中
        x = pdf.l_margin + (avail_w - disp_w) / 2
        y = pdf.t_margin + (avail_h - disp_h) / 2
        if probe.orientation == 1:
            pdf.image(probe.source, x=x, y=y, w=disp_w, h=disp_h)
        else:
            # 原始字节透传，EXIF 方向通过变换矩阵在显示层校正
            src_w = probe.src_width_px / probe.dpi[0] * 25.4
            src_h = probe.src_height_px / probe.dpi[1] * 25.4
            tf = _orientation_transform(probe.orientation, x, y, disp_w, disp_h, scale)
            with pdf.transform(tf):
                pdf.image(probe.source, x=0, y=0, w=src_w, h=src_h)

        if progress is not None:
            progress(index, total)

    output = Path(output)
    pdf.output(str(output))
    return output
