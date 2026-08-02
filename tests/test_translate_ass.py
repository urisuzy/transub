import unittest

from translate_ass import (
    ass_control_tokens,
    parse_ass,
    rebuild_ass_text,
)


ASS_SAMPLE = (
    "﻿[Script Info]\r\n"
    "Title: Fixture\r\n"
    "[V4+ Styles]\r\n"
    "Format: Name, Fontname\r\n"
    "Style: main,Arial\r\n"
    "[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Comment: 0,0:00:00.00,0:00:01.00,main,,0,0,0,,Do not translate\r\n"
    "Dialogue: 0,0:00:01.00,0:00:02.00,main,A,0,0,0,,"
    "{\\an8}Hello, world\\Nagain{\\b0}\r\n"
    "Dialogue: 0,0:00:02.00,0:00:03.00,sign_board,,0,0,0,,AUTHORIZED ONLY\r\n"
    "Dialogue: 0,0:00:03.00,0:00:04.00,main,,0,0,0,,"
    "{\\p1}m 0 0 l 10 10{\\p0}\r\n"
)


class AssParserTests(unittest.TestCase):
    def test_parse_preserves_structure_and_extracts_safe_dialogue(self):
        lines, dialogues = parse_ass(ASS_SAMPLE)

        self.assertEqual("".join(lines), ASS_SAMPLE)
        self.assertEqual(len(dialogues), 2)
        self.assertEqual(dialogues[0].source_text, "Hello, world again")
        self.assertEqual(dialogues[1].source_text, "AUTHORIZED ONLY")
        self.assertTrue(dialogues[1].prefix.endswith(","))

    def test_rebuild_preserves_control_tokens_exactly(self):
        _, dialogues = parse_ass(ASS_SAMPLE)
        rebuilt = rebuild_ass_text(
            dialogues[0],
            "Halo dunia lagi",
        )

        self.assertEqual(
            ass_control_tokens(rebuilt),
            [r"{\an8}", r"\N", r"{\b0}"],
        )
        self.assertEqual(
            ass_control_tokens(dialogues[0].original_text),
            ass_control_tokens(rebuilt),
        )
        self.assertEqual(
            rebuilt.replace(r"{\an8}", "")
            .replace(r"\N", " ")
            .replace(r"{\b0}", "")
            .split(),
            ["Halo", "dunia", "lagi"],
        )

    def test_unsafe_format_and_malformed_dialogue_are_not_selected(self):
        unsafe = (
            "[Script Info]\n[Events]\n"
            "Format: Layer, Text, Start\n"
            "Dialogue: 0,hello,0:00:00.00\n"
            "Dialogue: malformed\n"
            "Format: Layer, Text\n"
            "Dialogue: 0,{\\an8 unclosed text\n"
        )
        lines, dialogues = parse_ass(unsafe)

        self.assertEqual("".join(lines), unsafe)
        self.assertEqual(dialogues, [])

    def test_rebuild_preserves_whitespace_around_inline_tags(self):
        inline = (
            "[Script Info]\n[Events]\n"
            "Format: Layer, Text\n"
            "Dialogue: 0,Hello {\\b1}world{\\b0}\n"
        )
        _, dialogues = parse_ass(inline)

        rebuilt = rebuild_ass_text(dialogues[0], "Halo dunia")

        self.assertEqual(rebuilt, r"Halo {\b1}dunia{\b0}")

    def test_tokenizer_preserves_all_supported_ass_escapes(self):
        text = r"{\i1}one\ntwo\hthree\Nfour{\i0}"

        self.assertEqual(
            ass_control_tokens(text),
            [r"{\i1}", r"\n", r"\h", r"\N", r"{\i0}"],
        )


if __name__ == "__main__":
    unittest.main()
