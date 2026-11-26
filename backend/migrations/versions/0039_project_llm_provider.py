"""project llm provider: which gateway a workspace's own LLM key belongs to.

Nullable. NULL is OpenRouter, so every workspace that already saved a key keeps routing exactly as
before. `aimlapi` sends the same stored key (`openrouter_api_key_encrypted`) to AI/ML API's
OpenAI-compatible endpoint instead (see infrastructure/llm/provider.py).

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Same reasoning as 0015: additive and nullable, so IF NOT EXISTS keeps it idempotent.
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS llm_provider")
