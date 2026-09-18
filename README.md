# NOBORI Health Checkup Scraper (`nobori_scraper.py`)

A standalone Python script that uses Android Debug Bridge (`adb`) to scrape and export all medical health checkup records from the [**NOBORI** Android app](https://play.google.com/store/apps/details?id=ltd.nobori.phrforandroid) ([official site](https://nobori.me/), package `ltd.nobori.phrforandroid`) into structured JSON files.

It requires **zero external Python dependencies** (uses only the Python 3 standard library) and contains no hardcoded user or clinic data—making it usable for any patient, any medical institution, and any number of checkup years (including gap years).

---

## Prerequisites

1. **Python 3** installed on your computer.
2. **ADB (Android Debug Bridge)** installed and available in your system `PATH` (`adb --version`).
3. An **Android phone** with:
   - **Developer Options** and **USB Debugging** enabled.
   - The [**NOBORI** app](https://play.google.com/store/apps/details?id=ltd.nobori.phrforandroid) installed and logged in.

---

## Step-by-Step Setup & Usage

### 1. Connect and Prepare Your Phone
1. Connect your Android phone to your computer via a USB cable.
2. Unlock your phone and accept the **"Allow USB debugging"** prompt if it appears.
3. Verify the connection by running:
   ```bash
   adb devices
   ```
   Make sure your device appears in the list as `device` (not `unauthorized`).
4. Open the **NOBORI** app on your phone and navigate to the **"Health Checkups"** tab (`健診` / `Health Checkups`, Tab 3 of 3 under the main medical history screen) so that the list of your health checkups is visible on screen.
   - *Tip*: Keep your phone screen unlocked while the scraper runs.

### 2. Run the Scraper
Run the script from your terminal:

```bash
python3 nobori_scraper.py
```

#### What the script does automatically:
1. Scans the **Health Checkups** list screen and detects each checkup card.
2. Taps each checkup to open its detailed results screen.
3. Expands any truncated doctor/facility comments (`Read on` / `続きを読む`).
4. Scrolls step-by-step from top to bottom, saving UI hierarchy XML snapshots to `dumps/checkup_<YYYY-MM-DD>/`.
5. Navigates back to the list and scrolls down to discover and scrape any older checkups hidden further down the page.
6. Parses and stitches all UI steps into structured JSON files named `health_checkup_<YYYY-MM-DD>.json`.

---

## Command-Line Options

```bash
python3 nobori_scraper.py [OPTIONS]
```

| Option | Description |
| :--- | :--- |
| `--from-dumps` | Re-parse existing XML files in `--dumps-dir` into JSON without connecting to a phone via ADB. |
| `--dumps-dir DIR` | Directory where raw UI XML dumps are saved or read from (default: `dumps`). |
| `--output-dir DIR` | Directory where output JSON files are written (default: current directory `.`). |
| `-s SERIAL`, `--serial SERIAL` | Target a specific ADB device serial number if multiple Android devices are connected. |

### Example: Re-parsing Existing XML Dumps Offline
If you have already scraped the UI dumps and want to regenerate the JSON files offline without connecting your phone:

```bash
python3 nobori_scraper.py --from-dumps
```

---

## Output JSON Format

Each health checkup is saved as `health_checkup_<YYYY-MM-DD>.json` with the following structure:

```json
{
  "checkup_date": "2024-10-15",
  "checkup_date_display": "Tue, Oct 15, 2024",
  "facility": "Sample Medical Clinic",
  "checkup_type": "定期健康診断",
  "overall_evaluation": {
    "result": "B",
    "result_description": "年1回の健康診断で経過を見て下さい",
    "result_date": "10/15/2024",
    "previous_result": "A",
    "previous_result_date": "10/12/2023"
  },
  "judgement_type_definitions": {
    "A": "異常ありません",
    "B": "年1回の健康診断で経過を見て下さい",
    "C": "3～6ヶ月内の経過観察が必要です",
    "D2": "精密検査が必要です",
    "D1": "医療が必要です",
    "E": "主治医の指示に従って下さい"
  },
  "comment": "Mild elevation in LDL cholesterol noted. Please maintain a balanced diet and regular exercise.",
  "comment_lines": [
    "Mild elevation in LDL cholesterol noted. Please maintain a balanced diet and regular exercise."
  ],
  "follow_up_summary": [
    {
      "action_required": "経過観察",
      "descriptions": [
        "B : 年1回の健康診断で経過を見て下さい"
      ],
      "items": [
        {
          "category": "脂質代謝",
          "evaluation": "B"
        }
      ]
    }
  ],
  "guidance": [
    "受診後のご案内"
  ],
  "data_source": "Medical institution (10/22/2024 14:30)",
  "categories": [
    {
      "category": "身体計測",
      "evaluation": "A",
      "evaluation_description": "異常ありません",
      "previous_evaluation": "A",
      "items": [
        {
          "item": "身長",
          "result": "170.5",
          "previous_result": "170.4",
          "standard_value": null,
          "unit": "cm",
          "status": "normal",
          "out_of_range": false
        },
        {
          "item": "体重",
          "result": "64.2",
          "previous_result": "63.8",
          "standard_value": null,
          "unit": "kg",
          "status": "normal",
          "out_of_range": false
        }
      ]
    }
  ]
}
```

### Item Status Classification (`status`)
Each test item is automatically evaluated against its reference range and classified into one of the NOBORI UI's highlight categories:
- `"normal"`: Within standard reference range.
- `"over_standard_value"`: Above the upper reference limit (highlighted pink in the app UI).
- `"below_limit"`: Below the lower reference limit (highlighted blue in the app UI).
- `"abnormality"`: Qualitative finding or non-`A` evaluation grade (highlighted red in the app UI).
