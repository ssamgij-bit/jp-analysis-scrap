"""JPX 상장 종목 목록(코드→종목명) 갱신."""
import io, json, os
import openpyxl
from common import S, ROOT

URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"


def refresh_codes():
    r = S.get(URL, timeout=60)
    r.raise_for_status()
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True)
    out = {}
    for row in list(wb.active.iter_rows(values_only=True))[1:]:
        code, name, mkt = str(row[1]).strip(), str(row[2]).strip(), str(row[3] or "")
        if "株式" in mkt:
            out[code] = name
    if len(out) > 3000:
        with open(os.path.join(ROOT, "codes.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return len(out)


if __name__ == "__main__":
    print(refresh_codes())
