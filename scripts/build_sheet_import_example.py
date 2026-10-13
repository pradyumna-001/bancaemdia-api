"""Generate a small, entirely synthetic XLSX for the spreadsheet workflow."""

from pathlib import Path

from openpyxl import Workbook


def main() -> None:
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/sheet_import/synthetic-import.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    if sheet is None:
        raise RuntimeError("missing worksheet")
    sheet.title = "Apostas"
    sheet.append([
        "linha_id",
        "casa",
        "data_aposta",
        "odd",
        "stake_unidades",
        "atualizada_em",
        "data_jogo",
        "freebet",
        "descricao",
    ])
    sheet.append([
        "EXAMPLE-001",
        "Betano",
        "2026-09-20T12:00:00-03:00",
        2.0,
        1.0,
        "2026-10-12T12:00:00-03:00",
        "2026-09-22T12:00:00-03:00",
        False,
        "Casa x Fora — exemplo sintético",
    ])
    workbook.save(path)
    workbook.close()


if __name__ == "__main__":
    main()
