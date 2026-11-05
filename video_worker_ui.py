"""Video generation worker v2: UI automation with OpenCV slider puzzle solver."""
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
from browser import cookie_value, inject_region, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url

# Daily limit pattern matching response text
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day|"
    r"giới hạn.*(?:mỗi|hằng|hàng|trong) ngày|hạn mức.*(?:mỗi|hằng|hàng|trong) ngày|hết lượt tạo video",
    re.IGNORECASE,
)

# Content policy / safety filter rejection patterns
REJECTION_PATTERN = re.compile(
    r"申し訳ありません|お答えできません|生成することができません|生成できません|"
    r"利用規約|コンテンツポリシー|安全基準|倫理ガイドライン|不適切な表現|"
    r"肖像保護|実在する人物|実在の人物|肖像権|人物の画像|"
    r"肖像保护|实存在人物|真实人物|真人照片|"
    r"portrait protection|real person|likeness protection|"
    r"vi phạm|chính sách|tiêu chuẩn cộng đồng|nội dung nhạy cảm|không thể tạo|không thể sinh|không phù hợp|từ chối yêu cầu|"
    r"violat|content policy|community guideline|safety guideline|sensitive content|"
    r"unable to (?:generate|create)|cannot (?:generate|create)|against our policy|refuse to|inappropriate|"
    r"违反|违规|社区准则|敏感词|安全策略|无法生成|不能生成",
    re.IGNORECASE,
)

# Server busy / overload / temporary error patterns
SERVER_BUSY_PATTERN = re.compile(
    r"サーバーが混み合って|しばらくしてからもう一度|システムエラー|エラーが発生|"
    r"server is busy|too many requests|try again later|system busy|service unavailable|"
    r"服务器繁忙|请稍后重试|系统繁忙|生成失败|网络异常|"
    r"máy chủ quá tải|máy chủ đang bận|hệ thống bận|quá nhiều yêu cầu|vui lòng thử lại sau|lỗi hệ thống",
    re.IGNORECASE,
)


class AccountLimitedError(Exception):
    """Account reached daily video generation limit."""


class CreditInsufficientError(Exception):
    """Insufficient points prior to generation."""


class ContentPolicyError(Exception):
    """Dola safety/moderation policy rejected the prompt."""


class ServerBusyError(Exception):
    """Dola server is congested or temporarily unavailable."""


class GenerationFailedError(Exception):
    """Dola creation block failed or bot did not initiate video."""


VIDEO_BTN = "text=動画を作成"          # Entry point button in ja-JP locale

# ---- Dola UI labels per interface language (ja / vi / en). The UI language follows DOLA_LOCALE. ----
# "Create video" entry button in the composer toolbar
CREATE_VIDEO_LABELS = ("動画を作成", "Tạo video", "Create Videos", "Create Video", "生成视频", "创建视频")
# Model chip in video mode: "モデル 2.5" (ja) / "Model  Dreamina Seedance 2.5 ⌄" (en) / "Mô hình ..." (vi).
# Matches the label at the start; the full chip text (with the model name) is read from its button.
MODEL_CHIP_PATTERN = re.compile(r"^\s*(?:モデル|Model|Mô hình|模型)(?:\s|$)", re.IGNORECASE)
# "Fast" suffix of the 2.0 fast model in each language
FAST_PATTERN = re.compile(r"高速|fast|nhanh|快速", re.IGNORECASE)
# Combined aspect-ratio + duration chip of the current UI: "Auto · 10s", "16:9 · 15s", "Tự động · 10 giây"
SETTINGS_CHIP_PATTERN = re.compile(
    r"^\s*(?:auto|tự động|自動|自动|\d{1,2}\s*:\s*\d{1,2})\s*[·•・|]\s*\d{1,2}\s*(?:s|sec|giây|秒)\s*$",
    re.IGNORECASE,
)
# Older UI: separate aspect-ratio chip label
RATIO_LABELS = ("比率", "Tỷ lệ", "Tỉ lệ", "Tỷ lệ khung hình", "Aspect ratio", "Ratio")
# Older UI: standalone duration chip opening a dropdown ("10s", "10 giây", "10秒")
LEGACY_DURATION_CHIP_PATTERN = re.compile(r"^\s*\d{1,2}\s*(?:s|sec|giây|秒)\s*$", re.IGNORECASE)


def _duration_pattern(seconds: int):
    return re.compile(rf"(?<![\d.]){seconds}\s*(?:s|sec|giây|秒)(?![A-Za-z])", re.IGNORECASE)
# Bot reply that means "I am generating your video" (reply language follows the prompt language)
GEN_ACCEPTED_PATTERN = re.compile(
    r"生成された動画|動画を生成|動画を作成|"
    r"tạo video|video của bạn|đang tạo|"
    r"generat\w* (?:a |the |your )?video|creat\w* (?:a |the |your )?video|your video|"
    r"生成视频|正在生成|视频生成",
    re.IGNORECASE,
)
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # Captcha verifycenter iframe


# Read-only balance pre-check from recent conversations
BALANCE_JS = r"""
async ({msToken, fp}) => {
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.62", doubao_pc_version: "3.32.62",
    region: __DOLA_REGION__, sys_region: __DOLA_REGION__, samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });
  if (msToken) params.set("msToken", msToken);
  if (fp) params.set("fp", fp);
  const headers = {
    "Content-Type": "application/json; encoding=utf-8",
    "agw-js-conv": "str", "Accept": "*/*",
  };
  const recent = await fetch("/im/chain/recent_conv?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3200,
      uplink_body: {pull_recent_conv_chain_uplink_body: {
        limit: 20, message_count_per_conv: 10, api_version: 1, conv_version: 0,
        direction: 3,
        option: {not_need_message: false, need_complete_conversation: true,
          need_coco_conversation: true, need_coco_bot: true,
          need_pc_pin_chain: true, pc_pin_query_type: 0},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!recent.ok) return {ok: false, texts: []};
  const recentData = await recent.json();
  const body = recentData.downlink_body || {};
  const down = body.pull_recent_conv_chain_downlink_body || {};
  const cells = down.cells || [];
  const ids = cells.map(c => (c.conversation || {}).conversation_id || c.id)
    .filter(Boolean).slice(0, 10);
  if (!ids.length) return {ok: true, texts: []};

  const batch = await fetch("/im/chain/batch_single?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3101,
      uplink_body: {batch_pull_singe_chain_uplink_body: {
        conversation_type: 3, direction: 3, limit: 1,
        params: ids.map(conversation_id => ({conversation_id})),
        evaluate_ab_params: "", evaluate_common_params: "", ext: {},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!batch.ok) return {ok: true, texts: []};
  const data = await batch.json();
  const texts = [];
  const seen = new Set();
  const walk = (v) => {
    if (typeof v === "string") {
      if ((v.includes("ポイント") || v.includes("积分") || v.toLowerCase().includes("points")
        || v.includes("上限") || v.includes("limit")) && v.length < 1200 && !seen.has(v)) {
        seen.add(v); texts.push(v);
      }
      try {
        const t = v.trim();
        if (t.startsWith("{") || t.startsWith("[")) walk(JSON.parse(t));
      } catch (e) {}
      return;
    }
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) { for (const x of v) walk(x); return; }
    for (const x of Object.values(v)) walk(x);
  };
  walk(data);
  return {ok: true, texts: texts.slice(-80)};
}
"""


async def _first_visible(locator):
    """First visible element matched by a Playwright locator, or None."""
    try:
        for i in range(min(await locator.count(), 20)):
            item = locator.nth(i)
            if await item.is_visible():
                return item
    except Exception:
        pass
    return None


async def _outside(locator, container) -> bool:
    """True when `locator` is neither inside nor around `container` (an ElementHandle or None)."""
    if container is None:
        return True
    try:
        return not await locator.evaluate("(el, c) => c.contains(el) || el.contains(c)", container)
    except Exception:
        return True


async def _select_model(page, model_key: str):
    """Opens the model chip ("モデル 2.5" / "Model Dreamina Seedance 2.5" / "Mô hình ...") and picks the model."""
    chip, label = None, ""
    candidates = page.get_by_text(MODEL_CHIP_PATTERN)
    for i in range(min(await candidates.count(), 20)):
        loc = candidates.nth(i)
        if not await loc.is_visible():
            continue
        # Only a real control counts (chat messages may also start with "Model ..."); the label may be
        # its own span, so read the whole button to see the current model name.
        info = await loc.evaluate(
            "el => { const b = el.closest('button,[role=button],[role=combobox]');"
            " return {inButton: !!b, text: ((b || el).innerText || '').trim()}; }")
        if info["inButton"] and len(info["text"]) <= 80:
            chip, label = loc, info["text"]
            break
    if chip is None:
        raise RuntimeError(f"model chip not found (locale={config.LOCALE})")
    is_25 = "2.5" in label
    if (model_key == "seedance_v2.5") == is_25 and re.search(r"2\.[05]", label):
        print(f"[ui] Model already {model_key} ({label[:40]})", flush=True)
        return
    chip_handle = await chip.evaluate_handle(
        "el => el.closest('button,[role=button],[role=combobox]') || el")
    await chip.click(timeout=5000)
    await page.wait_for_timeout(600)
    wanted = r"Seedance\s*2\.5" if model_key == "seedance_v2.5" else r"Seedance\s*2\.0"
    candidates = page.get_by_text(re.compile(wanted, re.I))
    option = None
    for i in range(min(await candidates.count(), 20)):
        loc = candidates.nth(i)
        # Skip the chip itself (it also shows the model name)
        if not await loc.is_visible() or not await _outside(loc, chip_handle):
            continue
        if model_key == "seedance_v2.5":
            option = loc
            break
        option = option or loc
        if FAST_PATTERN.search((await loc.text_content()) or ""):  # prefer the fast 2.0 variant
            option = loc
            break
    if option is None:
        await page.keyboard.press("Escape")
        raise RuntimeError("model option not found in dropdown")
    await option.click(timeout=5000)
    await page.wait_for_timeout(500)
    print(f"[ui] Model set to {model_key}", flush=True)


async def _settings_chip(page):
    """The chip that opens aspect ratio / duration settings.

    Current UI: one combined chip "Auto · 10s" / "16:9 · 10s". Older UI: a separate "比率" chip."""
    chip = await _first_visible(page.get_by_text(SETTINGS_CHIP_PATTERN))
    if chip is None:
        for label in RATIO_LABELS:
            chip = await _first_visible(page.get_by_text(label, exact=True))
            if chip:
                break
    return chip


async def _open_settings(page) -> bool:
    chip = await _settings_chip(page)
    if chip is None:
        return False
    await chip.click(timeout=3000)
    await page.wait_for_timeout(600)
    return True


async def _select_ratio(page, ratio: str):
    """Picks an aspect ratio tile ("16:9", "9:16", ...) in the settings popover."""
    chip = await _first_visible(page.get_by_text(SETTINGS_CHIP_PATTERN))
    if chip and re.match(rf"^\s*{re.escape(ratio)}\s*[·•・|]", (await chip.text_content()) or ""):
        print(f"[ui] Ratio already {ratio}", flush=True)
        return
    option = await _first_visible(page.get_by_text(ratio, exact=True))
    if option is None:
        if not await _open_settings(page):
            raise RuntimeError(f"ratio / duration chip not found (locale={config.LOCALE})")
        option = await _first_visible(page.get_by_text(ratio, exact=True))
    if option is None:
        raise RuntimeError(f"ratio option {ratio} not found")
    await option.click(timeout=3000)
    await page.wait_for_timeout(400)
    print(f"[ui] Ratio set to {ratio}", flush=True)


async def _duration_option(page, seconds: int):
    """A clickable duration choice ("30s" / "30 giây" / "30秒") when Dola shows options instead of a slider."""
    exact = re.compile(rf"^\s*{seconds}\s*(?:s|sec|giây|秒)\s*$", re.IGNORECASE)
    chip = await _first_visible(page.get_by_text(SETTINGS_CHIP_PATTERN))
    chip_handle = await chip.element_handle() if chip else None
    candidates = page.get_by_text(exact)
    for i in range(min(await candidates.count(), 20)):
        loc = candidates.nth(i)
        if await loc.is_visible() and await _outside(loc, chip_handle):
            return loc
    return None


async def _set_slider(page, slider, seconds: int):
    """Moves a duration slider (role=slider, e.g. 4s..10s) to `seconds` with the keyboard."""
    def _num(v, default):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    lo = _num(await slider.get_attribute("aria-valuemin"), 0)
    hi = _num(await slider.get_attribute("aria-valuemax"), 0)
    if hi and seconds > hi:
        raise RuntimeError(f"slider max is {hi:g}s, cannot set {seconds}s (Dola30 extension not active?)")
    if lo and seconds < lo:
        raise RuntimeError(f"slider min is {lo:g}s, cannot set {seconds}s")
    await slider.focus()
    for _ in range(60):
        now = _num(await slider.get_attribute("aria-valuenow"), None)
        if now is None:
            raise RuntimeError("slider has no aria-valuenow")
        if abs(now - seconds) < 0.5:
            return
        before = now
        await page.keyboard.press("ArrowRight" if now < seconds else "ArrowLeft")
        await page.wait_for_timeout(60)
        if _num(await slider.get_attribute("aria-valuenow"), before) == before:
            raise RuntimeError("slider does not respond to the keyboard")
    raise RuntimeError("slider did not reach the target value")


async def _select_duration(page, seconds: int):
    """Sets the video duration: combined chip check -> option tiles -> slider -> legacy dropdown."""
    chip = await _first_visible(page.get_by_text(SETTINGS_CHIP_PATTERN))
    if chip and _duration_pattern(seconds).search(re.split(r"[·•・|]", (await chip.text_content()) or "")[-1]):
        print(f"[ui] Duration already {seconds}s", flush=True)
        return
    option = await _duration_option(page, seconds)
    slider = await _first_visible(page.get_by_role("slider"))
    if option is None and slider is None:
        if chip is not None or await _settings_chip(page) is not None:
            await _open_settings(page)
        else:
            # Legacy UI: a standalone "10s" chip opening a dropdown
            legacy = await _first_visible(page.get_by_text(LEGACY_DURATION_CHIP_PATTERN))
            if legacy is None:
                raise RuntimeError(f"duration control not found (locale={config.LOCALE})")
            await legacy.click(timeout=3000)
            await page.wait_for_timeout(500)
        option = await _duration_option(page, seconds)
        slider = await _first_visible(page.get_by_role("slider"))
    # A slider wins: its scale labels ("4s" ... "10s") look like options but are not clickable choices.
    if slider is not None:
        await _set_slider(page, slider, seconds)
    elif option is not None:
        await option.click(timeout=3000)
    else:
        raise RuntimeError(f"duration option {seconds}s not found")
    await page.wait_for_timeout(400)
    # Confirm on the combined chip when the UI has one
    chip = await _first_visible(page.get_by_text(SETTINGS_CHIP_PATTERN))
    if chip is not None:
        shown = re.split(r"[·•・|]", (await chip.text_content()) or "")[-1].strip()
        if not _duration_pattern(seconds).search(shown):
            raise RuntimeError(f"duration still shows {shown!r} after selecting {seconds}s")
    print(f"[ui] Duration set to {seconds}s", flush=True)


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.HTTP_PROXY or None) as r:
            return await r.read()



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Uploads reference images through native file input and waits for TOS upload."""
    if not image_paths:
        return

    # Dola hard limit: max 10 images per message
    if len(image_paths) > 10:
        print(f"[upload] Dola chỉ hỗ trợ tối đa 10 ảnh tham chiếu. Tự động lấy 10 ảnh đầu tiên (trong tổng số {len(image_paths)} ảnh).", flush=True)
        image_paths = image_paths[:10]

    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10000)
    events = []

    def on_response(response):
        url = response.url
        if "/alice/resource/prepare_upload" in url or "/upload/v1/" in url:
            events.append((response.status, url))

    page.on("response", on_response)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 15)
        last_change_time = time.time()
        last_count = 0

        while time.time() < deadline:
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)

            # 1. Đã upload đủ số lượng mong muốn lên TOS
            # Lưu ý: Dola gộp (batch) các yêu cầu prepare_upload nên prepare_count thường nhỏ hơn expected (chỉ 1-3 lần).
            # Chỉ số đo lường chính xác cho từng file là TOS upload (/upload/v1/).
            if tos_count >= expected:
                await page.wait_for_timeout(1000)
                print(f"[upload] Reference images uploaded successfully: {expected} image(s) (tos={tos_count}, prep={prepare_count})", flush=True)
                return

            # 2. Theo dõi nếu số lượng đã ổn định (Dola đã nạp hết số file chấp nhận)
            if tos_count != last_count:
                last_count = tos_count
                last_change_time = time.time()
            elif tos_count > 0 and (time.time() - last_change_time > 6):
                await page.wait_for_timeout(1000)
                print(f"[upload] Reference images stabilized at {tos_count}/{expected} image(s)", flush=True)
                return

            await page.wait_for_timeout(250)

        raise TimeoutError(
            f"Reference image upload timeout: tos={tos_count}/{expected}, prepare={prepare_count}"
        )
    finally:
        page.remove_listener("response", on_response)


def _gen_track(distance: float):
    """Generates humanized mouse drag trajectory."""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 9)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 22)))
    for i in range(1, random.randint(3, 5) + 1):
        pts.append((distance + overshoot * (1 - i / 5), random.uniform(-0.8, 0.8), random.randint(15, 30)))
    return pts


async def solve_slider(page, frame, attempt: int) -> bool:
    """Solves captcha slider notch within iframe and performs drag."""
    await frame.wait_for_selector("img", timeout=15000)
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(800)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")
    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"]), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"])), None)
    if not bg or not piece:
        print("  ✗ Captcha background or puzzle image not found", flush=True)
        return False

    bg_bytes = await _fetch_bytes(bg["src"])
    piece_bytes = await _fetch_bytes(piece["src"])
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552
    distance = (gap_x - (piece["left"] - bg["left"]) / scale) * scale
    print(f"  [solve#{attempt}] gap_x={gap_x} conf={conf:.3f} scale={scale:.2f} distance={distance:.0f}px", flush=True)

    btn = frame.locator(".captcha-slider-btn")
    bb = await btn.bounding_box()
    if not bb:
        print("  ✗ Drag handle .captcha-slider-btn not found", flush=True)
        return False
    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 350))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 180))
    for dx, dy, dt in _gen_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.up()

    for _ in range(10):
        await page.wait_for_timeout(700)
        if not find_captcha_frame(page):
            return True
    return False



_BALANCE_PATTERNS = (
    re.compile(r"(?:本日は|今日(?:还剩|剩余)?|今天).*?(\d+)\s*(?:ポイント|积分|points?)", re.I),
    re.compile(r"(?:remaining|left)\s*[:：]?\s*(\d+)\s*points?", re.I),
    re.compile(r"(?:还剩|剩余|还有)\s*(\d+)\s*(?:积分|点)", re.I),
    re.compile(r"còn(?: lại)?\s*(\d+)\s*điểm", re.I),
)


def _parse_balance_texts(texts: list[str]) -> tuple[int | None, bool, str]:
    for text in texts:
        if DAILY_LIMIT_PATTERN.search(text):
            return None, True, text
    for text in texts:
        for pattern in _BALANCE_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1)), False, text
    return None, False, ""


async def _preflight_balance(page, ms_token: str, fp: str, required: int) -> dict:
    """Reads known credit balance from chat history."""
    try:
        result = await asyncio.wait_for(page.evaluate(
            inject_region(BALANCE_JS), {"msToken": ms_token, "fp": fp}), timeout=30)
        balance, daily_limited, source = _parse_balance_texts(result.get("texts", []))
        if daily_limited:
            raise AccountLimitedError(f"Account daily generation limit: {source[:120]}")
        if balance is not None and balance < required:
            raise CreditInsufficientError(
                f"Insufficient points: current {balance}, required {required} (source: {source[:120]})"
            )
        return {"balance": balance, "source": source}
    except (AccountLimitedError, CreditInsufficientError):
        raise
    except Exception as e:
        print(f"  Balance pre-check indeterminate (proceeding with submit): {str(e)[:120]}", flush=True)
        return {"balance": None, "source": ""}


async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None, on_balance=None) -> dict:
    """Polls accepted conversation for video completion with fail-fast detection."""
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    last_callback = 0.0
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            poll = await asyncio.wait_for(page.evaluate(
                inject_region(POLL_JS), {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
        except Exception as e:
            print(f"  Polling exception: {e}", flush=True)
            continue
        now = time.time()
        elapsed = int(now - start)
        if on_poll and now - last_callback >= 30:
            on_poll(now)
            last_callback = now

        # 1. Early fail on explicit creation block failure
        failures = poll.get("failures", [])
        if failures:
            raise GenerationFailedError(f"Lỗi tạo video phía Dola: {failures[0]}")

        # 2. Check text responses for quota, policy rejection, or server errors
        for text in poll.get("texts", []):
            balance, _, source = _parse_balance_texts([text])
            if balance is not None and on_balance:
                on_balance(balance, source)
            if DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError(f"Tài khoản đã đạt giới hạn ngày: {text[:120]}")
            if CREDIT_FAIL_PATTERN.search(text):
                raise CreditError(f"Không đủ hạn mức/điểm: {text[:80]}")
            if REJECTION_PATTERN.search(text):
                raise ContentPolicyError(f"Dola từ chối prompt do vi phạm an toàn/chính sách: {text[:160]}")
            if SERVER_BUSY_PATTERN.search(text):
                raise ServerBusyError(f"Máy chủ Dola đang quá tải / bận: {text[:120]}")

        # 3. Detect conversational bot response without video block
        all_bot_text = " ".join(poll.get("botTexts", []))
        if all_bot_text:
            if REJECTION_PATTERN.search(all_bot_text):
                raise ContentPolicyError(f"Dola từ chối (chính sách an toàn / chân dung): {all_bot_text[:180]}")
            if SERVER_BUSY_PATTERN.search(all_bot_text):
                raise ServerBusyError(f"Máy chủ Dola quá tải: {all_bot_text[:120]}")

        # If after 35s there is no creation block
        if elapsed >= 35 and not poll.get("hasCreationBlock") and poll.get("botTexts"):
            # If the bot says it is generating the video, it was accepted; give up to 90s for creation block
            if GEN_ACCEPTED_PATTERN.search(all_bot_text):
                if elapsed >= 90:
                    raise GenerationFailedError(f"Dola chưa tạo video block sau 90s: '{poll['botTexts'][-1][:150]}'")
            else:
                raise GenerationFailedError(f"Dola không tạo video (Bot phản hồi: '{poll['botTexts'][-1][:150]}')")

        # 4. Check for completed video
        if poll.get("videos"):
            video_models = poll.get("videoModels", [])
            url = extract_unwatermarked_url(
                video_models[0] if video_models else "", poll["videos"][0])
            print(f"[{account}] Completed! Downloading (unwatermarked priority)...", flush=True)
            local = await _download(url, account)
            print(f"[{account}] Downloaded {local} ({local.stat().st_size / 1e6:.1f} MB)", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}

        prog = poll.get("creationProgress")
        prog_str = f" [tiến độ {prog}%]" if prog is not None else ""
        print(f"  ...Generating{prog_str} ({elapsed}s)", flush=True)
    raise TimeoutError(f"Không nhận được video trong {timeout}s (conversation_id={conversation_id})")


async def resume_video(account: str, conversation_id: str, timeout: int,
                       on_poll=None, on_balance=None) -> dict:
    """Recovers accepted session after server restart without re-sending prompt."""
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False, use_extension=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                            timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            return await poll_conversation(account, page, context, conversation_id, timeout, on_poll, on_balance)
        finally:
            await context.close()


def sanitize_prompt(text: str) -> str:
    """
    Removes duration mentions from prompt to prevent Dola's LLM chatbot from intercepting
    the request with conversational refusals (e.g. '30-second version not supported').
    Duration is injected via protocol / UI dropdown instead.
    """
    if not text:
        return text

    cleaned = text
    # 1. Phrases like 'with a duration of 30 seconds', 'lasting 30s', 'length of 30 seconds'
    cleaned = re.sub(
        r"(?i)\b(?:with\s+a\s+)?(?:duration|length)\s+(?:of\s+)?\d+[- ]*(?:seconds?|secs?|s|giây)\b",
        "",
        cleaned,
    )
    # 2. 'finish(ed)? at exactly \d+ seconds/s'
    cleaned = re.sub(
        r"(?i)\bfinish(?:ed)?\s+at\s+exactly\s+\d+[- ]*(?:seconds?|secs?|s|giây)\b",
        "",
        cleaned,
    )
    # 3. Time ranges like '0-5s', '15-30s', '0s-5s', '0 to 5 seconds', '(0-5s)'
    cleaned = re.sub(
        r"(?i)\(?\s*\b\d+\s*s?\s*[-–to]+\s*\d+\s*(?:seconds?|secs?|s|giây)\b\s*\)?",
        "",
        cleaned,
    )
    # 4. 'dài \d+ giây/s' or 'thời lượng \d+ giây/s'
    cleaned = re.sub(
        r"(?i)\b(?:với\s+)?(?:dài|thời lượng)\s+\d+[- ]*(?:giây|s)\b",
        "",
        cleaned,
    )
    # 5. '\d+[- ]*(second|seconds|sec|giây)' e.g. '30-second', '30 seconds', '15 second', '30 giây'
    cleaned = re.sub(
        r"(?i)\b\d+[- ]*(?:seconds?|secs?|giây)\b",
        "",
        cleaned,
    )
    # 6. Standalone '\d+s' like '30s', '15s', '10s'
    cleaned = re.sub(
        r"(?i)(?<=\s)\d+s\b|^\d+s\b|\b\d+s(?=\s|[.,;:!?\)])",
        "",
        cleaned,
    )
    # Remove empty parens left by removed timestamps
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    # Clean up spaces before punctuation
    cleaned = re.sub(r"\s+([,.:;?!])", r"\1", cleaned)
    cleaned = re.sub(r"([,;])\s*([,;])", r"\1", cleaned)
    # Clean up double spaces while preserving line breaks
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(lines).strip()


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool = True,
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths: list[str] | None = None) -> dict:
    """Full generation flow via UI automation."""
    prompt_to_send = sanitize_prompt(prompt)
    if prompt_to_send != prompt:
        print(f"[{account}] Prompt sanitized to prevent Dola LLM duration refusal", flush=True)
    timeout = timeout or config.VIDEO_TIMEOUT
    model_key = model.lower().replace("-", "_")
    if model_key in ("seedance_2.5", "seedance_v2.5", "seedance_25", "seedance_v25"):
        model_key = "seedance_v2.5"
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20"):
        model_key = "seedance_v2.0"
    else:
        raise ValueError(f"Unsupported model: {model} (supported: seedance-2.0 / seedance-2.5)")
    if duration is not None and duration not in (10, 15, 30):
        raise ValueError("Dola supports durations of 10s, 15s, and 30s via extension")
    if duration == 30 and not use_extension:
        raise ValueError("30s generation requires Dola30 extension enabled")
    # 30s videos require extended generation timeout
    if duration == 30:
        timeout = max(timeout, 1800)
    if model_key == "seedance_v2.5":
        timeout = max(timeout, 600)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            # ---- UI Submission ----
            clicked_video_btn = False
            for label in CREATE_VIDEO_LABELS:
                loc = page.get_by_text(label, exact=True).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    clicked_video_btn = True
                    break
            if not clicked_video_btn:
                print(f"[{account}] WARNING: 'Create video' button not found "
                      f"(locale={config.LOCALE}); continuing in current mode", flush=True)
            await page.wait_for_timeout(1500)
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)
            # Select model, aspect ratio and duration in the video composer
            try:
                await _select_model(page, model_key)
            except Exception as e:
                print(f"[{account}] WARNING: failed to set model {model_key}, Dola default is used: "
                      f"{str(e)[:120]}", flush=True)
            if ratio:
                try:
                    await _select_ratio(page, ratio)
                except Exception as e:
                    print(f"[{account}] WARNING: failed to set ratio {ratio}, Dola default is used: "
                          f"{str(e)[:120]}", flush=True)
            if duration:
                try:
                    await _select_duration(page, duration)
                except Exception as e:
                    print(f"[{account}] WARNING: failed to set duration {duration}s, Dola default is used: "
                          f"{str(e)[:120]}", flush=True)

            box = await page.query_selector("textarea") or await page.query_selector('[contenteditable="true"]')
            if not box:
                raise RuntimeError("Không tìm thấy ô nhập prompt trên giao diện Dola")
            await box.click()
            await page.wait_for_timeout(150)

            # Fast paste / insert prompt (instant instead of character-by-character 100ms delay)
            try:
                await page.evaluate("""async (text) => {
                    try {
                        if (navigator.clipboard && navigator.clipboard.writeText) {
                            await navigator.clipboard.writeText(text);
                            return true;
                        }
                    } catch (e) {}
                    return false;
                }""", prompt_to_send)
                await page.keyboard.press("Control+v")
                await page.wait_for_timeout(200)
            except Exception:
                pass

            content_in_box = await page.evaluate(
                """(el) => (el.value || el.innerText || el.textContent || '').trim()""", box
            )
            if not content_in_box:
                try:
                    await page.keyboard.insert_text(prompt_to_send)
                    await page.wait_for_timeout(200)
                except Exception:
                    pass

            content_in_box = await page.evaluate(
                """(el) => (el.value || el.innerText || el.textContent || '').trim()""", box
            )
            if not content_in_box:
                try:
                    tag = await box.evaluate("(el) => el.tagName.toLowerCase()")
                    if tag in ("textarea", "input"):
                        await box.fill(prompt_to_send)
                    else:
                        await page.keyboard.type(prompt_to_send, delay=1)
                except Exception:
                    await page.keyboard.type(prompt_to_send, delay=2)

            await page.wait_for_timeout(400)
            await page.keyboard.press("Enter")
            print(f"[{account}] UI submitted prompt: {prompt_to_send[:40]}", flush=True)

            # ---- Captcha Solver (up to 3 attempts) ----
            solved_or_absent = False
            for attempt in range(1, 4):
                frame = None
                for _ in range(20):
                    await page.wait_for_timeout(1000)
                    frame = find_captcha_frame(page)
                    if frame:
                        break
                if not frame:
                    solved_or_absent = True
                    break
                print(f"[{account}] Captcha detected, attempt {attempt} solving...", flush=True)
                if await solve_slider(page, frame, attempt):
                    print(f"[{account}] Captcha passed ✓", flush=True)
                    await page.wait_for_timeout(3000)  # Wait for frontend auto-retry
                    solved_or_absent = True
                    break
                print(f"[{account}] Captcha not passed, retrying...", flush=True)
            if not solved_or_absent:
                await page.screenshot(path="solve_fail.png")
                raise RiskControlError("Captcha failed 3 times")

            # ---- Wait for real conversation_id ----
            conv_id = ""
            for step_i in range(30):
                await page.wait_for_timeout(1000)
                tail = page.url.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    conv_id = tail
                    break
                if step_i == 4 and not conv_id:
                    try:
                        send_btn = page.locator('button[type="submit"], [aria-label*="send" i], [aria-label*="Send" i], button:has(svg)').last
                        if await send_btn.count() and await send_btn.is_visible():
                            await send_btn.click(timeout=1000)
                    except Exception:
                        pass
            if not conv_id:
                await page.screenshot(path="no_conv.png")
                raise TimeoutError("conversation_id not acquired within 30s")
            print(f"[{account}] conversation_id={conv_id}, polling for video...", flush=True)

            deadline = time.time() + timeout
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            return await poll_conversation(account, page, context, conv_id, timeout, on_poll, on_balance)
        finally:
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "An orange cat napping on a sunny windowsill"
    ratio = sys.argv[3] if len(sys.argv) > 3 else None
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else None
    model = sys.argv[5] if len(sys.argv) > 5 else "seedance_v2.0"
    result = await generate_video(account, prompt, ratio, duration, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        import traceback
        traceback.print_exc()