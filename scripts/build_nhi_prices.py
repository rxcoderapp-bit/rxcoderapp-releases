# -*- coding: utf-8 -*-
"""Standalone NHIA Open Data JSON builder for GitHub Actions."""
import codecs
import csv
import datetime
import json
import os
import sys
import urllib.request

NHI_CSV_URL = "https://info.nhi.gov.tw/api/iode0000s01/Dataset?rId=A21030000I-E41001-001"
USER_AGENT = "RXcoderapp-NHI-Actions/1.0"


def _roc_today_int():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return (now.year - 1911) * 10000 + now.month * 100 + now.day


def _parse_roc_int(val):
    s = (val or "").strip()
    return int(s) if s and s.isdigit() else 0


def _clean_float(val):
    s = (val or "").strip()
    if not s:
        return None
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def _roc_str_to_iso(roc_s):
    s = (roc_s or "").strip()
    if not s or not s.isdigit() or s == "9991231":
        return ""
    val = int(s)
    y = val // 10000 + 1911
    m = (val % 10000) // 100
    d = val % 100
    if 1 <= m <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return ""


class _StringTable:
    def __init__(self):
        self.values = []
        self._index = {}

    def intern(self, text):
        t = (text or "").strip()
        if not t:
            return -1
        idx = self._index.get(t)
        if idx is None:
            idx = len(self.values)
            self._index[t] = idx
            self.values.append(t)
        return idx


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "nhi_prices.json"
    req = urllib.request.Request(NHI_CSV_URL, headers={"User-Agent": USER_AGENT})
    today_roc = _roc_today_int()
    recent_cutoff_roc = today_roc - 20000

    code_history = {}
    code_meta = {}
    row_count = 0

    with urllib.request.urlopen(req, timeout=180) as resp:
        reader = csv.reader(codecs.getreader("utf-8-sig")(resp))
        header = next(reader, None)
        if not header:
            raise RuntimeError("Empty NHIA CSV response")
        col = {name.strip(): i for i, name in enumerate(header)}

        idx_code = col["藥品代號"]
        idx_en = col.get("藥品英文名稱", -1)
        idx_zh = col.get("藥品中文名稱", -1)
        idx_ing = col.get("成分", -1)
        idx_price = col["支付價"]
        idx_start = col["有效起日"]
        idx_end = col["有效迄日"]
        idx_vendor = col.get("藥商", -1)
        idx_form = col.get("劑型", -1)
        idx_class = col.get("藥品分類", -1)
        idx_group = col["分類分組名稱"]
        idx_atc = col["ATC代碼"]
        idx_link = col.get("藥品代碼超連結", -1)

        for row in reader:
            row_count += 1
            if len(row) <= idx_atc:
                continue
            code = row[idx_code].strip()
            price = _clean_float(row[idx_price])
            s_int = _parse_roc_int(row[idx_start])
            e_int = _parse_roc_int(row[idx_end])
            if not code or price is None or s_int == 0:
                continue

            code_history.setdefault(code, []).append((s_int, e_int, price))
            prev_meta = code_meta.get(code)
            if prev_meta is None or (s_int, e_int) >= prev_meta[0]:
                lic_url = row[idx_link].strip() if 0 <= idx_link < len(row) else ""
                lic_id = lic_url.split("licId=")[-1].strip() if "licId=" in lic_url else ""
                code_meta[code] = (
                    (s_int, e_int),
                    row[idx_zh].strip() if idx_zh >= 0 else "",
                    row[idx_en].strip() if idx_en >= 0 else "",
                    row[idx_ing].strip() if idx_ing >= 0 else "",
                    row[idx_vendor].strip() if idx_vendor >= 0 else "",
                    row[idx_form].strip() if idx_form >= 0 else "",
                    row[idx_class].strip() if idx_class >= 0 else "",
                    row[idx_group].strip(),
                    row[idx_atc].strip(),
                    lic_id,
                )

    groups, vendors, forms, classes = _StringTable(), _StringTable(), _StringTable(), _StringTable()
    items = {}
    active_count = 0
    changed_count = 0

    for code, intervals in code_history.items():
        intervals.sort(key=lambda x: (x[0], x[1]))
        current = intervals[-1]
        for iv in intervals:
            if iv[0] <= today_roc <= (iv[1] if iv[1] > 0 else 9991231):
                current = iv

        latest_start, latest_end, latest_price = current
        is_active = (latest_price > 0) and (latest_end == 0 or latest_end >= today_roc)
        if (not is_active and latest_start < recent_cutoff_roc) or (latest_price <= 0 and latest_start < recent_cutoff_roc):
            continue

        prev_price = None
        for iv in reversed(intervals):
            if iv[0] < latest_start and iv[2] != latest_price and iv[2] > 0:
                prev_price = iv[2]
                break
        if prev_price is not None:
            changed_count += 1
        if is_active:
            active_count += 1

        _, zh, en, ing, vendor, form, drug_class, group_name, atc, lic_id = code_meta[code]
        entry = {
            "p": latest_price,
            "ed": _roc_str_to_iso(str(latest_start)),
            "a": atc,
            "g": groups.intern(group_name),
            "c": classes.intern(drug_class),
            "f": forms.intern(form),
            "v": vendors.intern(vendor),
            "z": zh,
            "e": en,
            "i": ing,
        }
        if prev_price is not None:
            entry["pp"] = prev_price
        if lic_id:
            entry["l"] = lic_id
        if not is_active:
            entry["act"] = 0
        items[code] = entry

    now_tw = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "meta": {
            "updated_at": now_tw,
            "source": NHI_CSV_URL,
            "total_csv_rows": row_count,
            "item_count": len(items),
            "active_count": active_count,
            "price_changed_count": changed_count,
            "group_count": len(groups.values),
        },
        "groups": groups.values,
        "classes": classes.values,
        "forms": forms.values,
        "vendors": vendors.values,
        "items": items,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(encoded)
    if os.path.exists(out_path):
        os.remove(out_path)
    os.rename(tmp, out_path)
    print(f"Built {out_path}: {len(encoded):,} bytes ({len(items)} items)")


if __name__ == "__main__":
    main()
