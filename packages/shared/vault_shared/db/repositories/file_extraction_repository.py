import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from vault_shared.db.models import FileExtraction


class FileExtractionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_file_id(self, file_id: uuid.UUID) -> FileExtraction | None:
        return self._session.get(FileExtraction, file_id)

    def upsert(
        self,
        *,
        file_id: uuid.UUID,
        status: str,
        extractor_name: str | None,
        extracted_text: str | None,
        char_count: int | None,
        error: str | None,
        extracted_at: datetime,
    ) -> FileExtraction:
        """`extracted_at` only moves when what was extracted changed — every
        analysis downstream (AI intelligence, entities) re-runs when it
        moves, and re-reading an unchanged file on a rescan is no reason to
        pay for that again."""
        extraction = self.get_by_file_id(file_id)
        if extraction is None:
            extraction = FileExtraction(file_id=file_id)
            self._session.add(extraction)
        elif extraction.status == status and extraction.extracted_text == extracted_text:
            extracted_at = extraction.extracted_at or extracted_at

        extraction.status = status
        extraction.extractor_name = extractor_name
        extraction.extracted_text = extracted_text
        extraction.char_count = char_count
        extraction.error = error
        extraction.extracted_at = extracted_at
        self._session.flush()
        return extraction
