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

# 심판(역분류·관련성 판정) 전용 제공사. 비우면 LLM_*을 그대로 씀.
# 생성 모델과 다른 제공사로 두면 "자기 글을 자기가 채점하는" 편향이 사라진다.
JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL") or LLM_BASE_URL
JUDGE_API_KEY = os.getenv("JUDGE_API_KEY") or LLM_API_KEY
JUDGE_MODEL = os.getenv("JUDGE_MODEL") or LLM_MODEL_SMALL
JUDGE_MIN_INTERVAL = float(os.getenv("JUDGE_MIN_INTERVAL", "0"))

# 임베딩: sentence-transformers 로컬 모델 (기본 bge-m3, 다국어)
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"
EMBED_MODEL = os.getenv("EMBED_MODEL") or DEFAULT_EMBED_MODEL
EMBED_DEVICE = os.getenv("EMBED_DEVICE") or None   # 비우면 sentence-transformers 자동(MPS 되면 MPS). MPS 에러 시 cpu
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "32"))

# 벡터 DB: QDRANT_URL 없으면 로컬 파일 모드 (Docker 불필요)
QDRANT_URL = os.getenv("QDRANT_URL") or None
QDRANT_PATH = str(ROOT / ".qdrant")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# 문서 표현 모드 (rag/documents.doc_text): full = 설비+증상+조치 기록+조치 결과, symptom_only = 증상만
DOC_TEXT_MODE = os.getenv("DOC_TEXT_MODE", "full")

# 컬렉션 이름에 임베딩 모델(+문서 모드)을 박아두어 모델·표현 교체 사고를 방지. full은 기존 이름 유지
COLLECTION = f"{DOMAIN}_{_slug(EMBED_MODEL)}" + ("" if DOC_TEXT_MODE == "full" else f"_{DOC_TEXT_MODE}") + "_v1"
TOP_K = int(os.getenv("TOP_K", "5"))
