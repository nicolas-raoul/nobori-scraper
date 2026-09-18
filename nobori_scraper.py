#!/usr/bin/env python3
"""
NOBORI Health Checkup Scraper & Parser

Connects via ADB to an Android phone running the NOBORI app on the "Health Checkups" tab,
scrolls through all available health checkups across any number of years (including gap years),
dumps the UI hierarchy for each checkup (expanding any truncated comments), and exports
each health checkup as a structured JSON file.

Usage:
  # Scrape live from a connected Android phone via ADB:
  python3 nobori_scraper.py

  # Re-parse existing XML dumps without an ADB connection:
  python3 nobori_scraper.py --from-dumps
"""

import argparse
from datetime import datetime
import glob
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET


JUDGEMENT_TYPE_DEFINITIONS = {
    "A": "異常ありません",
    "B": "年1回の健康診断で経過を見て下さい",
    "C": "3～6ヶ月内の経過観察が必要です",
    "D2": "精密検査が必要です",
    "D1": "医療が必要です",
    "E": "主治医の指示に従って下さい",
}

UI_IGNORE_TOKENS = {
    "Back",
    "戻る",
    "TOP",
    "トップ",
    "上へ",
}


class AdbController:
    """Helper class to interact with an Android device via ADB."""

    def __init__(self, serial=None):
        self.serial = serial

    def _cmd(self, *args):
        cmd = ["adb"]
        if self.serial:
            cmd.extend(["-s", self.serial])
        cmd.extend(args)
        return cmd

    def run(self, *args, check=True):
        return subprocess.run(
            self._cmd(*args),
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def check_device(self):
        """Verify that an Android device is connected and authorized via ADB."""
        res = self.run("devices", check=False)
        lines = [
            line.strip()
            for line in (res.stdout or "").splitlines()
            if line.strip() and not line.startswith("List of devices")
        ]
        if not lines:
            raise RuntimeError(
                "No Android device detected via ADB. Please connect a phone via USB "
                "with USB debugging enabled, or run with '--from-dumps' to parse existing XML dumps."
            )
        if any("unauthorized" in line for line in lines):
            raise RuntimeError(
                "Android device is unauthorized. Please accept the 'Allow USB debugging' prompt on the phone."
            )

    def dump_ui(self, local_path, retries=3):
        """Dump current UI hierarchy from device to local_path with retry logic."""
        remote_path = "/sdcard/window_dump.xml"
        for attempt in range(retries):
            res = self.run("shell", "uiautomator", "dump", remote_path, check=False)
            if res.returncode == 0:
                self.run("pull", remote_path, local_path)
                return
            # Wake screen and wait briefly before retrying
            self.run("shell", "input", "keyevent", "KEYCODE_WAKEUP", check=False)
            time.sleep(1.0)
        raise RuntimeError(
            f"Failed to dump UI hierarchy after {retries} attempts: {res.stderr}"
        )

    def tap(self, x, y, sleep_sec=1.5):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    def swipe(self, x1, y1, x2, y2, duration_ms=400, sleep_sec=1.2):
        self.run(
            "shell",
            "input",
            "swipe",
            str(int(x1)),
            str(int(y1)),
            str(int(x2)),
            str(int(y2)),
            str(int(duration_ms)),
        )
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    def press_back(self, sleep_sec=1.5):
        self.run("shell", "input", "keyevent", "4")
        if sleep_sec > 0:
            time.sleep(sleep_sec)


def parse_bounds(bounds_str):
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2) and center (cx, cy)."""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str or "")
    if not m:
        return (0, 0, 0, 0, 0, 0)
    x1, y1, x2, y2 = map(int, m.groups())
    return x1, y1, x2, y2, (x1 + x2) // 2, (y1 + y2) // 2


def parse_date_string(text):
    """
    Extract a normalized YYYY-MM-DD date string from various display formats:
    - 'Wed, Jan 15, 2020' or 'Jan 15, 2020'
    - '2020/01/15' or '1/15/2020'
    - '2020年1月15日'
    """
    if not text:
        return None

    clean = text.strip()
    # Remove leading day of week like 'Fri, ' or 'Wed, '
    clean_no_dow = re.sub(r"^[A-Za-z]{3},\s*", "", clean)

    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(clean_no_dow, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Japanese format: YYYY年M月D日
    m_jp = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", clean)
    if m_jp:
        return f"{int(m_jp.group(1)):04d}-{int(m_jp.group(2)):02d}-{int(m_jp.group(3)):02d}"

    # Slash or dash date inside string: YYYY/MM/DD or MM/DD/YYYY
    m_ymd = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", clean)
    if m_ymd:
        return f"{int(m_ymd.group(1)):04d}-{int(m_ymd.group(2)):02d}-{int(m_ymd.group(3)):02d}"

    m_mdy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", clean)
    if m_mdy:
        return f"{int(m_mdy.group(3)):04d}-{int(m_mdy.group(1)):02d}-{int(m_mdy.group(2)):02d}"

    return None


def is_checkup_card(content_desc):
    """Determine if a UI element on the list screen represents a Health Checkup card."""
    if not content_desc or "\n" not in content_desc:
        return False
    lines = [line.strip() for line in content_desc.split("\n") if line.strip()]
    if len(lines) < 3:
        return False
    if any(
        kw in content_desc
        for kw in ("Overall evaluation", "総合判定", "Evaluation")
    ):
        return True
    if parse_date_string(lines[0]) is not None:
        return True
    return False


def find_checkup_cards_in_xml(xml_path):
    """Return a list of (content_desc, center_x, center_y, y1, y2) for checkup cards on screen."""
    root = ET.parse(xml_path).getroot()
    cards = []
    for el in root.iter():
        if el.attrib.get("clickable") != "true":
            continue
        cd = el.attrib.get("content-desc", "")
        if is_checkup_card(cd):
            x1, y1, x2, y2, cx, cy = parse_bounds(el.attrib.get("bounds", ""))
            # Ensure card is within the visible scroll area (below tabs, above bottom nav)
            if 300 <= cy <= 2100:
                cards.append((cd, cx, cy, y1, y2))
    return cards


def scrape_single_checkup_detail(adb, dump_dir):
    """
    Inside a single health checkup screen:
    1. Expand 'Read on' / '続きを読む' comment if present.
    2. Scroll down step-by-step and save XML dumps until the bottom is reached.
    """
    os.makedirs(dump_dir, exist_ok=True)
    init_xml = os.path.join(dump_dir, "init_check.xml")
    adb.dump_ui(init_xml)

    # Check if there is a 'Read on' button to expand truncated comments
    root = ET.parse(init_xml).getroot()
    for el in root.iter():
        cd = el.attrib.get("content-desc", "")
        if cd in ("Read on", "続きを読む"):
            _, _, _, _, cx, cy = parse_bounds(el.attrib.get("bounds", ""))
            if cx > 0 and cy > 0:
                print(f"    Expanding comment ('{cd}')...")
                adb.tap(cx, cy, sleep_sec=1.2)
            break

    if os.path.exists(init_xml):
        os.remove(init_xml)

    # Scroll through the checkup detail view and dump each step
    prev_items = None
    step = 0
    while True:
        step_file = os.path.join(dump_dir, f"step_{step:02d}.xml")
        adb.dump_ui(step_file)

        step_root = ET.parse(step_file).getroot()
        curr_items = [
            el.attrib.get("content-desc", "")
            for el in step_root.iter()
            if el.attrib.get("content-desc", "")
        ]

        print(f"    Step {step:02d}: captured {len(curr_items)} UI nodes")
        if prev_items is not None and curr_items == prev_items:
            # Remove duplicate final step file
            os.remove(step_file)
            print("    Reached bottom of checkup detail screen.")
            break

        prev_items = curr_items
        step += 1
        adb.swipe(540, 1800, 540, 600, duration_ms=400, sleep_sec=1.2)


def scrape_all_checkups_via_adb(adb, base_dumps_dir):
    """
    Discover and scrape all health checkups on the Health Checkups list tab,
    scrolling down to reveal any checkups hidden off-screen.
    """
    os.makedirs(base_dumps_dir, exist_ok=True)
    visited_cards = set()
    scraped_folders = []

    print("Scanning 'Health Checkups' tab for checkup records...")
    list_step = 0
    while True:
        list_xml = os.path.join(base_dumps_dir, "temp_list.xml")
        adb.dump_ui(list_xml)
        cards = find_checkup_cards_in_xml(list_xml)
        if os.path.exists(list_xml):
            os.remove(list_xml)

        unvisited = [c for c in cards if c[0] not in visited_cards]
        if unvisited:
            # Tap the first unvisited checkup card on screen
            cd, cx, cy, _, _ = unvisited[0]
            first_line = cd.split("\n")[0].strip()
            iso_date = parse_date_string(first_line) or f"checkup_{len(visited_cards) + 1:02d}"
            folder_name = f"checkup_{iso_date}"
            dump_subdir = os.path.join(base_dumps_dir, folder_name)

            print(f"\n[{len(visited_cards) + 1}] Opening checkup: {first_line} -> {folder_name}")
            adb.tap(cx, cy, sleep_sec=2.0)

            scrape_single_checkup_detail(adb, dump_subdir)
            scraped_folders.append((iso_date, dump_subdir))
            visited_cards.add(cd)

            print("    Returning to Health Checkups list...")
            adb.press_back(sleep_sec=2.0)
            continue

        # All currently visible cards have been visited; scroll down the list to check for more
        prev_card_descs = [c[0] for c in cards]
        adb.swipe(540, 1600, 540, 600, duration_ms=400, sleep_sec=1.5)

        list_xml_after = os.path.join(base_dumps_dir, "temp_list_after.xml")
        adb.dump_ui(list_xml_after)
        cards_after = find_checkup_cards_in_xml(list_xml_after)
        if os.path.exists(list_xml_after):
            os.remove(list_xml_after)

        curr_card_descs = [c[0] for c in cards_after]
        new_unvisited = [c for c in cards_after if c[0] not in visited_cards]

        if not new_unvisited and curr_card_descs == prev_card_descs:
            print("\nFinished scanning all health checkups on list.")
            break

        list_step += 1
        if list_step > 50:
            break

    return scraped_folders


def parse_dump_folder(folder):
    """Stitch all step_*.xml files in a folder into a single ordered sequence of content-descs."""
    files = sorted(glob.glob(os.path.join(folder, "step_*.xml")))
    if not files:
        raise FileNotFoundError(f"No step_*.xml files found in {folder}")

    first_root = ET.parse(files[0]).getroot()
    first_descs = [
        el.attrib.get("content-desc", "")
        for el in first_root.iter()
        if el.attrib.get("content-desc", "")
    ]

    # Extract fixed header metadata dynamically (elements between 'Back' and 'Overall evaluation')
    header_fields = []
    for desc in first_descs:
        if desc in ("Back", "戻る"):
            continue
        if desc in ("Overall evaluation", "総合判定", "Results in PDF", "PDFで結果を見る"):
            break
        header_fields.append(desc)

    date_display = header_fields[0] if len(header_fields) > 0 else ""
    facility = header_fields[1] if len(header_fields) > 1 else ""
    checkup_type = header_fields[2] if len(header_fields) > 2 else ""

    ignore_fixed = set(UI_IGNORE_TOKENS) | set(header_fields)

    steps_nodes = []
    for f in files:
        root = ET.parse(f).getroot()
        nodes = []
        for el in root.iter():
            cd = el.attrib.get("content-desc", "")
            if not cd or cd in ignore_fixed:
                continue
            nodes.append(cd)
        steps_nodes.append(nodes)

    full_seq = list(steps_nodes[0])
    for step_idx in range(1, len(steps_nodes)):
        prev = steps_nodes[step_idx - 1]
        curr = steps_nodes[step_idx]
        found = False
        for start_in_prev in range(len(prev)):
            sub_prev = prev[start_in_prev:]
            for start_in_curr in range(min(6, len(curr))):
                sub_curr = curr[start_in_curr : start_in_curr + len(sub_prev)]
                if len(sub_prev) >= 1 and sub_prev == sub_curr:
                    to_add = curr[start_in_curr + len(sub_prev) :]
                    full_seq.extend(to_add)
                    found = True
                    break
            if found:
                break
        if not found:
            # Fallback if no exact multi-element match: append non-overlapping tail
            for k in range(len(curr), 0, -1):
                if curr[:k] == full_seq[-k:]:
                    full_seq.extend(curr[k:])
                    found = True
                    break
            if not found:
                full_seq.extend(curr)

    return {
        "date_display": date_display,
        "facility": facility,
        "checkup_type": checkup_type,
        "seq": full_seq,
    }


def parse_bracket(s):
    """
    Parse '[Standard value , Unit]' bracket strings dynamically.
    Examples:
      '[10～20 , mmHg]' -> ('10～20', 'mmHg')
      '[cm]'            -> (None, 'cm')
      '[18.5～24.9]'    -> ('18.5～24.9', None)
      '[－,＋－]'       -> ('－,＋－', None)
    """
    inner = s[1:-1].strip()
    if " , " in inner:
        std, unit = inner.split(" , ", 1)
        return std.strip() or None, unit.strip() or None

    # Check if inner looks like a reference range or qualitative standard value
    range_indicators = ("～", "以上", "以下", "未満", "異常なし", "＋", "－", "±")
    if any(ind in inner for ind in range_indicators):
        return inner, None

    # If purely numeric, treat as standard value
    try:
        float(inner.replace(",", ""))
        return inner, None
    except ValueError:
        pass

    return None, inner


def evaluate_item_status(val_str, std_str):
    """
    Classify an item into the NOBORI UI's highlight categories:
    - 'over_standard_value' (Item over the standard value)
    - 'below_limit' (Item below the limit)
    - 'abnormality' (Items at abnormality)
    - 'normal'
    """
    if not val_str or val_str == "-":
        return "normal", False

    # Check qualitative abnormality grade in parentheses, e.g., '洞不整脈(B)'
    m_grade = re.search(r"\(([A-E][12]?)\)$", val_str)
    if m_grade:
        grade = m_grade.group(1)
        if grade != "A":
            return "abnormality", True
        return "normal", False

    if not std_str:
        return "normal", False

    num_str = val_str.replace(",", "").strip()
    if num_str.endswith("未満"):
        try:
            val = float(num_str[:-2]) - 1e-6
        except ValueError:
            return "normal", False
    elif num_str.endswith("以上"):
        try:
            val = float(num_str[:-2])
        except ValueError:
            return "normal", False
    else:
        try:
            val = float(num_str)
        except ValueError:
            if std_str in ("異常なし", "－") and val_str != std_str:
                return "abnormality", True
            return "normal", False

    std_clean = std_str.replace(",", "").strip()
    if "～" in std_clean:
        low_s, high_s = std_clean.split("～", 1)
        low_s, high_s = low_s.strip(), high_s.strip()
        if low_s:
            try:
                if val < float(low_s):
                    return "below_limit", True
            except ValueError:
                pass
        if high_s:
            try:
                if val > float(high_s):
                    return "over_standard_value", True
            except ValueError:
                pass
        return "normal", False

    if std_clean.endswith("以上"):
        try:
            if val < float(std_clean[:-2]):
                return "below_limit", True
        except ValueError:
            pass
        return "normal", False

    if std_clean.endswith("以下"):
        try:
            if val > float(std_clean[:-2]):
                return "over_standard_value", True
        except ValueError:
            pass
        return "normal", False

    if std_clean.endswith("未満"):
        try:
            if val >= float(std_clean[:-2]):
                return "over_standard_value", True
        except ValueError:
            pass
        return "normal", False

    return "normal", False


def build_checkup_json(folder, checkup_id=None):
    """Parse a checkup dump folder into a structured dictionary."""
    data = parse_dump_folder(folder)
    seq = data["seq"]

    # Determine ISO checkup date dynamically from header or folder name
    iso_date = parse_date_string(data["date_display"])
    if not iso_date and checkup_id:
        iso_date = parse_date_string(checkup_id) or checkup_id

    # Locate the start of the medical items table
    item_header_idx = None
    for idx, tok in enumerate(seq):
        if tok in ("Item", "項目"):
            item_header_idx = idx
            break
    if item_header_idx is None:
        raise ValueError(f"Could not find 'Item' table header in {folder}")

    header_seq = seq[:item_header_idx]

    overall_result = None
    overall_result_date = None
    overall_prev_result = None
    overall_prev_date = None
    comment = ""
    follow_up_summary = []

    i = 0
    while i < len(header_seq):
        token = header_seq[i]
        if token.startswith("Result\n") or token in ("Result", "今回"):
            m = re.search(r"\((.*?)\)", token)
            overall_result_date = m.group(1) if m else None
            if not iso_date and overall_result_date:
                iso_date = parse_date_string(overall_result_date)
            if i + 1 < len(header_seq):
                overall_result = header_seq[i + 1]
            i += 2
        elif token.startswith("Previous result") or token.startswith("前回"):
            m = re.search(r"\((.*?)\)", token)
            overall_prev_date = m.group(1) if m else None
            if i + 1 < len(header_seq):
                overall_prev_result = header_seq[i + 1]
            i += 2
        elif token in ("Comment", "コメント"):
            if i + 1 < len(header_seq):
                comment = header_seq[i + 1]
            i += 2
        elif any(
            token.startswith(prefix)
            for prefix in ("要精密検査", "要再検査", "要治療", "経過観察")
        ):
            group = {
                "action_required": token,
                "descriptions": [],
                "items": [],
            }
            i += 1
            while i < len(header_seq) and not any(
                header_seq[i].startswith(prefix)
                for prefix in ("要精密検査", "要再検査", "要治療", "経過観察")
            ):
                sub = header_seq[i]
                if "\n" in sub:
                    cat_name, grade = sub.split("\n", 1)
                    group["items"].append(
                        {"category": cat_name, "evaluation": grade}
                    )
                else:
                    group["descriptions"].append(sub)
                i += 1
            follow_up_summary.append(group)
        else:
            i += 1

    # Detect whether this checkup includes a previous result column
    has_prev = overall_prev_result not in (None, "-")

    start_idx = item_header_idx + 1
    for idx in range(item_header_idx, min(item_header_idx + 6, len(seq))):
        if seq[idx].startswith("Previous result") or seq[idx].startswith("前回"):
            start_idx = idx + 1
            break

    footer_markers = {
        "Item over the standard value",
        "Item below the limit",
        "Items at abnormality",
        "Guidance from the medical institutions",
        "医療機関からのご案内",
        "Data Source: ",
    }

    categories = []
    curr_cat_obj = None

    i = start_idx
    while i < len(seq):
        x = seq[i]
        if x in footer_markers:
            break
        if "\n" not in x:
            # Check for category header with both current & previous evaluations:
            # [CategoryName, Grade, '(Result)', PrevGrade, '(Previous result)']
            if (
                i + 4 < len(seq)
                and seq[i + 2] in ("(Result)", "(今回)")
                and seq[i + 4] in ("(Previous result)", "(前回)")
            ):
                curr_cat_obj = {
                    "category": seq[i],
                    "evaluation": seq[i + 1],
                    "evaluation_description": JUDGEMENT_TYPE_DEFINITIONS.get(
                        seq[i + 1]
                    ),
                    "previous_evaluation": seq[i + 3],
                    "items": [],
                }
                categories.append(curr_cat_obj)
                i += 5
                continue
            # Check for category header with single evaluation:
            # [CategoryName, Grade, '(Result)']
            elif i + 2 < len(seq) and seq[i + 2] in ("(Result)", "(今回)"):
                curr_cat_obj = {
                    "category": seq[i],
                    "evaluation": seq[i + 1],
                    "evaluation_description": JUDGEMENT_TYPE_DEFINITIONS.get(
                        seq[i + 1]
                    ),
                    "previous_evaluation": None,
                    "items": [],
                }
                categories.append(curr_cat_obj)
                i += 3
                continue
            i += 1
        else:
            lines = x.split("\n")
            item_name = lines[0]
            std_val, unit = None, None
            if lines[-1].startswith("[") and lines[-1].endswith("]"):
                std_val, unit = parse_bracket(lines[-1])
                val_lines = lines[1:-1]
            else:
                val_lines = lines[1:]

            res_val = val_lines[0] if len(val_lines) >= 1 else None
            prev_val = (
                val_lines[1] if (has_prev and len(val_lines) >= 2) else None
            )

            status, out_of_range = evaluate_item_status(res_val, std_val)

            item_obj = {
                "item": item_name,
                "result": res_val,
                "previous_result": prev_val,
                "standard_value": std_val,
                "unit": unit,
                "status": status,
                "out_of_range": out_of_range,
            }
            if curr_cat_obj is not None:
                curr_cat_obj["items"].append(item_obj)
            i += 1

    footer_seq = seq[i:]
    guidance = []
    data_source = None
    for idx, tok in enumerate(footer_seq):
        if tok in ("Guidance from the medical institutions", "医療機関からのご案内"):
            j = idx + 1
            while j < len(footer_seq) and not footer_seq[j].startswith("Data Source"):
                guidance.append(footer_seq[j])
                j += 1
        elif tok.startswith("Data Source") and idx + 1 < len(footer_seq):
            data_source = footer_seq[idx + 1]

    return {
        "checkup_date": iso_date or data["date_display"],
        "checkup_date_display": data["date_display"],
        "facility": data["facility"],
        "checkup_type": data["checkup_type"],
        "overall_evaluation": {
            "result": overall_result,
            "result_description": JUDGEMENT_TYPE_DEFINITIONS.get(overall_result),
            "result_date": overall_result_date,
            "previous_result": (
                overall_prev_result if overall_prev_result != "-" else None
            ),
            "previous_result_date": overall_prev_date,
        },
        "judgement_type_definitions": JUDGEMENT_TYPE_DEFINITIONS,
        "comment": comment,
        "comment_lines": [line for line in comment.split("\n") if line.strip()],
        "follow_up_summary": follow_up_summary,
        "guidance": guidance,
        "data_source": data_source,
        "categories": categories,
    }


def discover_existing_dump_folders(base_dumps_dir):
    """Find all checkup dump folders inside base_dumps_dir."""
    if not os.path.isdir(base_dumps_dir):
        return []
    folders = []
    for entry in sorted(os.listdir(base_dumps_dir)):
        full_path = os.path.join(base_dumps_dir, entry)
        if os.path.isdir(full_path) and glob.glob(os.path.join(full_path, "step_*.xml")):
            checkup_id = entry.replace("checkup_", "")
            folders.append((checkup_id, full_path))
    return folders


def main():
    parser = argparse.ArgumentParser(
        description="Scrape and parse NOBORI Health Checkups via ADB into JSON files."
    )
    parser.add_argument(
        "--from-dumps",
        action="store_true",
        help="Parse existing XML dumps in --dumps-dir without connecting via ADB.",
    )
    parser.add_argument(
        "--dumps-dir",
        default="dumps",
        help="Directory to store/read UI XML dumps (default: dumps).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write output JSON files (default: current directory).",
    )
    parser.add_argument(
        "-s",
        "--serial",
        default=None,
        help="Optional ADB device serial number.",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    if args.from_dumps:
        folders = discover_existing_dump_folders(args.dumps_dir)
        if not folders:
            print(f"No existing dump folders found in '{args.dumps_dir}'.")
            return
    else:
        adb = AdbController(serial=args.serial)
        try:
            adb.check_device()
            folders = scrape_all_checkups_via_adb(adb, args.dumps_dir)
        except RuntimeError as e:
            print(f"Error: {e}")
            return

    for checkup_id, folder in folders:
        data = build_checkup_json(folder, checkup_id=checkup_id)
        date_key = data["checkup_date"] or checkup_id
        out_path = os.path.join(args.output_dir, f"health_checkup_{date_key}.json")

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        total_items = sum(len(c["items"]) for c in data["categories"])
        flagged = sum(
            1
            for c in data["categories"]
            for item in c["items"]
            if item["out_of_range"]
        )
        print(
            f"Saved {out_path} ({len(data['categories'])} categories, {total_items} items, {flagged} flagged)"
        )


if __name__ == "__main__":
    main()
