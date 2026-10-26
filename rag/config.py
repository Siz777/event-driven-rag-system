"""환경 설정. 도메인 전환은 DOMAIN 환경변수 하나로."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = os.getenv("DOMAIN", "manufacturing")
DOMAIN_DIR = ROOT / "domains" / DOMAIN
DATA_DIR = DOMAIN_DIR / "data"
EVAL_DIR = DOMAIN_DIR / "eval"
PROMPT_DIR = DOMAIN_DIR / "prompts"

# LLM: OpenAI 호환 엔드포인트면 무엇이든 (OpenAI, Gemini OpenAI-compat, Ollama, vLLM ...)
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or None
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_MODEL_SMALL = os.getenv("LLM_MODEL_SMALL", LLM_MODEL)  # 검증·판정용 저렴한 모델

# 임베딩: sentence-transformers 로컬 모델 (기본 bge-m3, 다국어)
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "32"))

# 벡터 DB: QDRANT_URL 없으면 로컬 파일 모드 (Docker 불필요)
QDRANT_URL = os.getenv("QDRANT_URL") or None
QDRANT_PATH = str(ROOT / ".qdrant")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# 컬렉션 이름에 임베딩 모델을 박아두어 모델 교체 사고를 방지
COLLECTION = f"{DOMAIN}_{_slug(EMBED_MODEL)}_v1"
TOP_K = int(os.getenv("TOP_K", "5"))
