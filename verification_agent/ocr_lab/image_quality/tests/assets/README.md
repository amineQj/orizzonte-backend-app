# Synthetic test assets

`arabic_id_specimen.png` is a synthetic image generated only for OCR tests.
It contains the public labels `SPECIMEN / TEST`, `FICTIONAL DOCUMENT - NOT A REAL ID`,
`الجمهورية الجزائرية الديمقراطية الشعبية`, and `بطاقة التعريف الوطنية`.

It contains no personal data and is not a replica of a real document. The image
is pre-rendered because the test environment's Pillow build does not include
libraqm for Arabic shaping.
