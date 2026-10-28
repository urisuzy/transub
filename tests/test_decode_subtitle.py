import codecs
import unittest

import translate


def _srt(lines):
    return "".join(
        f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{line}\n\n"
        for i, line in enumerate(lines, start=1)
    )


ITALIAN = _srt([
    "Perché sei venuto qui?", "È già tardi, però possiamo parlarne.",
    "Non lo so, è difficile da spiegare.", "Città, università, libertà.",
    "Più tardi andiamo al caffè?", "Sì, così parliamo di tutto.",
] * 3)
ARABIC = _srt([
    "هل أنت بخير، يا سيدي؟", "بالطبع، ما الذي تفعله هنا؟",
    "أرسلت لك رسالة، ألم تقرأها؟", "آسف، هل كنتم تتناولون الطعام؟",
    "كلا، لم نأكل شيئًا بعد.", "كان بوسعنا أن نلتقي بالخارج.",
] * 3)


class DecodeSubtitleTests(unittest.TestCase):
    def test_utf8_with_and_without_bom(self):
        self.assertEqual(translate.decode_subtitle(ITALIAN.encode("utf-8")), ITALIAN)
        self.assertEqual(translate.decode_subtitle(codecs.BOM_UTF8 + ARABIC.encode("utf-8")), "\ufeff" + ARABIC)

    def test_utf16_with_bom(self):
        self.assertEqual(translate.decode_subtitle(ARABIC.encode("utf-16")), ARABIC)

    def test_legacy_latin_encoding(self):
        self.assertEqual(translate.decode_subtitle(ITALIAN.encode("cp1252")), ITALIAN)

    def test_legacy_central_european_encoding(self):
        romanian = _srt([
            "Ţi-am scris mesaj. Nu l-ai primit?", "Scuze, eraţi la masă?",
            "Nu, nu mâncam. Ce mai faci?", "Ne puteam întâlni undeva, de ce ai venit până aici?",
            "Datorită ăsteia. Este cam grea.", "Şi ce e asta? E o piatră adusă de bunicul meu.",
            "Mulţumesc foarte mult, e frumoasă.", "Familia ta trebuie să fie mândră de tine.",
            "Pleacă în străinătate pentru studii.", "Ştiu că eşti un student bun, ai încredere în mine.",
            "Fiica lor are nevoie de un profesor de engleză.", "Poţi să-i dai lecţii în locul meu?",
            "Nu sunt student la universitate.", "Asta nu contează, ştii engleză mai bine decât ei.",
        ])
        self.assertEqual(translate.decode_subtitle(romanian.encode("cp1250")), romanian)

    def test_legacy_arabic_encoding(self):
        self.assertEqual(translate.decode_subtitle(ARABIC.encode("cp1256")), ARABIC)


if __name__ == "__main__":
    unittest.main()
