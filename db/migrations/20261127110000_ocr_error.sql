-- migrate:up

-- Why OCR of this file last failed (NULL: it did not). A failure of the file itself (tesseract can't read
-- a page, a page takes too long, the PDF can't be drawn) is lasting: ocr_at is set with it, and the file is
-- not tried again by itself. Another (the process reading it killed, out of memory) leaves ocr_at NULL: the
-- next refresh tries it again, after the files never tried. Either way, a trial whose file failed OCR no
-- longer waits for it before a check under new rules (jobs/refresh.py, core.trials_waiting_for_ocr).
ALTER TABLE core.file ADD COLUMN ocr_error text;

-- migrate:down

ALTER TABLE core.file DROP COLUMN ocr_error;
