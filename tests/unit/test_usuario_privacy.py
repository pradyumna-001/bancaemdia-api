from io import BytesIO

from openpyxl import load_workbook

from bancaemdia.api.v1.usuario import EXPORT_TABLES, _excel, _safe_excel_value


def test_excel_export_escapes_spreadsheet_formulas() -> None:
    assert _safe_excel_value('=HYPERLINK("https://bad.test")') == (
        '\'=HYPERLINK("https://bad.test")'
    )
    assert _safe_excel_value("+cmd") == "'+cmd"
    data = {"usuario": {"nome": "=1+1"}, **{name: [] for name in EXPORT_TABLES}}
    workbook = load_workbook(BytesIO(_excel(data)), read_only=True)
    assert workbook["usuario"]["A2"].value == "'=1+1"
    assert workbook["usuario"]["A2"].data_type == "s"
