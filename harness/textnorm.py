"""Turkish-aware text normalisation used by every scorer.

Python's str.lower()/upper() are wrong for Turkish (I->i, i->I, and İ.lower()
yields 'i' + U+0307).  Everything that compares Turkish text goes through the
helpers here so that scoring is identical across suites.
"""
from __future__ import annotations

import re
import unicodedata

_TR_LOWER_MAP = str.maketrans({"I": "ı", "İ": "i"})
_TR_UPPER_MAP = str.maketrans({"i": "İ", "ı": "I"})
_ASCII_FOLD = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u",
    "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U", "Â": "A", "Î": "I", "Û": "U",
})
TR_SPECIAL_CHARS = "çğıöşüÇĞİÖŞÜ"
_WS = re.compile(r"\s+")


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def tr_lower(s: str) -> str:
    return nfc(s).translate(_TR_LOWER_MAP).lower()


def tr_upper(s: str) -> str:
    return nfc(s).translate(_TR_UPPER_MAP).upper()


def norm_ws(s: str) -> str:
    return _WS.sub(" ", s).strip()


def casefold_tr(s: str) -> str:
    """Turkish-aware lower-case + NFC + whitespace collapse."""
    return norm_ws(tr_lower(s))


def ascii_fold(s: str) -> str:
    """Remove Turkish diacritics (used only for lenient matching, never for headline scores)."""
    return nfc(s).translate(_ASCII_FOLD)


# --------------------------------------------------------------------------- numbers
_ONES = ["", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz"]
_TENS = ["", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan"]


def _below_thousand(n: int) -> list[str]:
    parts: list[str] = []
    h, rest = divmod(n, 100)
    t, o = divmod(rest, 10)
    if h:
        parts += (["yüz"] if h == 1 else [_ONES[h], "yüz"])
    if t:
        parts.append(_TENS[t])
    if o:
        parts.append(_ONES[o])
    return parts


def tr_number_words(n: int) -> list[str]:
    """Turkish words for 0 <= n < 10**12 as a token list ('1250' -> ['bin','iki','yüz','elli'])."""
    if n == 0:
        return ["sıfır"]
    if n < 0:
        return ["eksi"] + tr_number_words(-n)
    words: list[str] = []
    for value, name in ((10**9, "milyar"), (10**6, "milyon"), (10**3, "bin")):
        q, n = divmod(n, value)
        if q:
            if value == 1000 and q == 1:
                words.append("bin")
            else:
                words += _below_thousand(q) + [name]
    words += _below_thousand(n)
    return words


def number_variants(num: str) -> set[str]:
    """All surface forms we accept for a number appearing in a gold key fact.

    '270'  -> {'270', 'iki yüz yetmiş', 'ikiyüzyetmiş'}
    '7,5'  -> {'7,5', '7.5', 'yedi buçuk', 'yedibuçuk'}
    '1.000.000' / '1000000' -> grouped and ungrouped digits + words
    """
    s = num.strip()
    out = {s}
    if re.fullmatch(r"\d+|\d{1,3}(?:\.\d{3})+|\d{1,3}(?: \d{3})+", s):
        n = int(s.replace(".", "").replace(" ", ""))
        out.add(str(n))
        out.add(f"{n:,}".replace(",", "."))   # Turkish thousands separator
        if n > 1:                              # 'bir' (= a/one) and 'sıfır' would match almost any Turkish text
            w = tr_number_words(n)
            out.add(" ".join(w))
            out.add("".join(w))
    m = re.fullmatch(r"(\d+)[,.](\d+)", s)
    if m and not re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        a, b = m.group(1), m.group(2)
        out |= {f"{a},{b}", f"{a}.{b}"}
        if b == "5":
            w = tr_number_words(int(a))
            out |= {" ".join(w) + " buçuk", "".join(w) + "buçuk"}
    return {casefold_tr(x) for x in out if x}


_CITATION = re.compile(r"\[\s*K\s*\d+(?:\s*[,;]\s*K?\s*\d+)*\s*\]", re.I)


def contains_number(text: str, num: str) -> bool:
    t = casefold_tr(_CITATION.sub(" ", text or ""))       # '[K14]' is a citation label, not the number 14
    for v in number_variants(num):
        if re.fullmatch(r"[\d.,]+", v):
            # digits must not be glued to other digits (avoid '14' matching '140')
            if re.search(rf"(?<![\d.,]){re.escape(v)}(?![\d]|[.,]\d)", t):
                return True
        elif re.search(rf"(?<![^\W\d_]){re.escape(v)}(?![^\W\d_])", t):
            return True
    return False


NUMBER_IN_TEXT = re.compile(r"\d+(?:[.,]\d+)*")


# --------------------------------------------------------------------------- words / sentences
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


def words(s: str) -> list[str]:
    return WORD_RE.findall(s)


def word_count(s: str) -> int:
    return len(words(s))


_TITLES = {"dr", "prof", "doç", "av", "sn", "yrd", "öğr", "gör", "st", "no", "md", "s", "bkz", "örn", "mah", "cad",
           "sok", "tel", "fak"}                       # never end a sentence ('Dr. Ayşe', 'md. 17')
_ABBREV = {"vb", "vs", "vd", "ltd", "şti", "a.ş", "inc", "dk", "sa", "çev", "ed", "yy"}   # end one only before a capital


def sentences(s: str) -> list[str]:
    """Sentence split that keeps Turkish ordinals ('15. madde', '3. kişi') and abbreviations ('md. 17',
    'Dr. Ayşe') inside the sentence.  'N.' and 'vb.' still end a sentence when the next word is capitalised."""
    s = norm_ws(s)
    if not s:
        return []
    out, start = [], 0
    for m in re.finditer(r"[.!?…]+\s+", s):
        token = re.search(r"([^\s(\[]+)$", s[start:m.start()])
        word = tr_lower(token.group(1) if token else "").rstrip(".")
        nxt = s[m.end():m.end() + 1]
        if s[m.start():m.end()].strip() == ".":
            if word in _TITLES:
                continue
            if (word.isdigit() or word in _ABBREV) and not nxt.isupper():
                continue
        out.append(s[start:m.end()].strip())
        start = m.end()
    tail = s[start:].strip()
    if tail:
        out.append(tail)
    return [p for p in out if re.search(r"[^\W_]", p)]


# --------------------------------------------------------------------------- language heuristic
TR_STOP = set("""ve ile bir bu da de için olarak daha çok gibi ama ancak veya ya her bu şu o ne
mi mı mu mü değil var yok olan olduğu kadar sonra önce göre ise ki çünkü hem bile sadece
yalnızca tüm bütün bazı biz siz onlar ben sen kendi şey nasıl neden niçin hangi en""".split())
EN_STOP = set("""the and of to in is that for it with as on be at by this are or from an
was were which have has not but they you we can will would should there their what about""".split())


def turkish_ratio(s: str) -> float:
    toks = [tr_lower(w) for w in words(s)]
    tr = sum(t in TR_STOP for t in toks)
    en = sum(t in EN_STOP for t in toks)
    if tr + en == 0:
        return 1.0 if any(c in s for c in TR_SPECIAL_CHARS) else 0.0
    return tr / (tr + en)


# --------------------------------------------------------------------------- markup stripping (OCR / ASR)
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.M)
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_LATEX_INLINE = re.compile(r"\$+([^$]*)\$+")


def strip_markup(s: str) -> str:
    """Reduce OCR/LLM output to plain reading-order text (tables -> space separated cells)."""
    s = nfc(s)
    s = re.sub(r"```[a-zA-Z]*", "", s)
    s = _MD_IMG.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(td|th)>", " ", s, flags=re.I)
    s = re.sub(r"</(tr|p|div|h\d|li)>", "\n", s, flags=re.I)
    s = _HTML_TAG.sub(" ", s)
    s = _MD_TABLE_SEP.sub("", s)
    s = s.replace("|", " ")
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"(\*\*|__|~~)", "", s)
    s = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", s)
    s = re.sub(r"^\s*[-*•]\s+", "", s, flags=re.M)
    s = _LATEX_INLINE.sub(r"\1", s)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(ent, ch)
    return s


def norm_for_cer(s: str, *, casefold: bool = False) -> str:
    s = norm_ws(strip_markup(s))
    return tr_lower(s) if casefold else s


_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)


def norm_for_wer(s: str, *, numbers_to_words: bool = False) -> str:
    """ASR scoring normalisation: Turkish lower-case, apostrophes joined, punctuation removed."""
    s = tr_lower(nfc(s))
    s = re.sub(r"['’`´]", "", s)                     # İstanbul'da -> istanbulda
    if numbers_to_words:
        # Turkish grouping first ('1.250' = bin iki yüz elli), then decimals ('7,5' / '7.5' = yedi virgül beş)
        s = re.sub(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+(?![\d]|[.,]\d)", lambda m: m.group().replace(".", ""), s)
        s = re.sub(r"(\d+)[.,](\d+)", lambda m: m.group(1) + " virgül " + m.group(2), s)
        s = re.sub(r"\d+", lambda m: " ".join(tr_number_words(int(m.group()))), s)
    s = _PUNCT.sub(" ", s)
    return norm_ws(s)
