"""Tests for productivity.obsidian — the read-only Obsidian Kanban reader."""

from acta.productivity import obsidian as KO

SAMPLE = """---
updated: 2026-07-27
kanban-plugin: board
---

## Próximos

- [ ] Renew the passport


## Backlog

- [ ] **Draft** the project outline [[ideas]]
- [x] Already done

## Detalhado

- [ ] Card with a body
  first body line
  second body line
- [ ] Plain card

## Vazio



%% kanban:settings
```
{"kanban-plugin":"board","list-collapse":[false,null,false,true]}
```
%%
"""


def test_lane_names_and_order():
    b = KO.parse_text(SAMPLE)
    assert [l["name"] for l in b["lanes"]] == \
        ["Próximos", "Backlog", "Detalhado", "Vazio"]


def test_card_parsing_and_checked():
    b = KO.parse_text(SAMPLE)
    neg = b["lanes"][1]
    assert [c["text"] for c in neg["cards"]] == \
        ["**Draft** the project outline [[ideas]]", "Already done"]
    assert neg["cards"][0]["checked"] is False
    assert neg["cards"][1]["checked"] is True


def test_card_body_captured():
    b = KO.parse_text(SAMPLE)
    det = b["lanes"][2]
    assert det["cards"][0]["text"] == "Card with a body"
    assert det["cards"][0]["body"] == "first body line\nsecond body line"
    assert det["cards"][1]["body"] == ""


def test_empty_lane():
    b = KO.parse_text(SAMPLE)
    assert b["lanes"][3]["name"] == "Vazio"
    assert b["lanes"][3]["cards"] == []


def test_collapse_flags():
    b = KO.parse_text(SAMPLE)
    flags = [l["collapsed"] for l in b["lanes"]]
    assert flags == [False, False, False, True]   # null -> falsy


def test_settings_block_not_leaking_as_a_card():
    b = KO.parse_text(SAMPLE)
    all_text = " ".join(c["text"] for l in b["lanes"] for c in l["cards"])
    assert "kanban:settings" not in all_text
    assert "list-collapse" not in all_text


def test_frontmatter():
    b = KO.parse_text(SAMPLE)
    assert b["frontmatter"].get("kanban-plugin") == "board"


def test_no_frontmatter_is_tolerated():
    b = KO.parse_text("## Solo\n\n- [ ] one\n")
    assert b["lanes"][0]["name"] == "Solo"
    assert b["lanes"][0]["cards"][0]["text"] == "one"


def test_unconfigured_vault_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(KO, "VAULT", None)
    b = KO.parse_board()
    assert b["lanes"] == [] and "no Obsidian vault" in b["error"]


def test_real_file_is_read(tmp_path, monkeypatch):
    (tmp_path / "Kanban.md").write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setattr(KO, "VAULT", tmp_path)
    b = KO.parse_board("Kanban.md")
    assert "error" not in b and b["lanes"][0]["name"] == "Próximos"
