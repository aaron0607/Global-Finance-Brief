"""成稿人名简称规则。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_brief
from content_validation import apply_name_abbreviations, name_abbreviation_errors


class NameAbbreviationTests(unittest.TestCase):
    def test_replaces_everywhere_except_sources(self):
        content = {
            "main_title": "习近平访美·中美削减关税",
            "alert": "国家主席习近平与美方会晤",
            "sections": [{"id": "focus", "bullets": ["习近平主席表示", "无关"], "note": "习近平"}],
            "sources": [{"source": "Reuters", "title": "习近平 visit", "published_at": "x"}],
        }
        out = apply_name_abbreviations(content)
        self.assertEqual(out["main_title"], "习访美·中美削减关税")
        self.assertEqual(out["alert"], "国家主席习与美方会晤")
        self.assertEqual(out["sections"][0]["bullets"], ["习主席表示", "无关"])
        self.assertEqual(out["sections"][0]["note"], "习")
        self.assertEqual(out["sources"][0]["title"], "习近平 visit")
        self.assertEqual(content["alert"], "国家主席习近平与美方会晤")  # 不改原对象

    def test_final_render_blocks_full_name(self):
        self.assertEqual(name_abbreviation_errors("国家主席习与美方会晤"), [])
        self.assertTrue(name_abbreviation_errors("国家主席习近平与美方会晤"))

    def test_build_rewrites_content_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content_20260927.json"
            path.write_text(json.dumps({"alert": "习近平", "sections": []}, ensure_ascii=False), encoding="utf-8")
            with mock.patch("sys.argv", ["build_brief.py", str(path)]), \
                    mock.patch.object(build_brief, "validate_no_structure_leaks", side_effect=SystemExit("stop")), \
                    mock.patch("builtins.print"):
                with self.assertRaises(SystemExit):
                    build_brief.main()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["alert"], "习")


class ReaderFontSizeTests(unittest.TestCase):
    """正文可读性：段落 16px，逻辑快读/表格/导读不低于 15px。"""

    def test_bullet_and_note_sizes(self):
        self.assertIn("font-size:16px", build_brief.bullet("测试正文。", "#000000"))
        self.assertIn("font-size:15px", build_brief.note_bar("测试。", "red", "#000000"))

    def test_tables_not_below_15px(self):
        row = {"price": "1", "chg": "+1%", "dir": "up"}
        html = build_brief.render_index_table("标签", [row] * len(build_brief.INDEX_NAMES))
        self.assertIn("font-size:15px", html)
        cal = build_brief.render_calendar(
            [{"date": "10月12日 周一", "event": "20:30 事件", "stars": 3}])
        self.assertIn("font-size:15px", cal)


if __name__ == "__main__":
    unittest.main()
