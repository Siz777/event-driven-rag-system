"""rag.embed — 모델별 질의 프롬프트 분기(모델 로드 없음)."""
from rag import config, embed


def test_default_model_is_bge_m3_and_collection_name_unchanged():
    assert config.DEFAULT_EMBED_MODEL == "BAAI/bge-m3"
    assert f"manufacturing_{config._slug(config.DEFAULT_EMBED_MODEL)}_v1" == "manufacturing_baai_bge_m3_v1"


def test_collection_slug_per_model():
    assert config._slug("Qwen/Qwen3-Embedding-0.6B") == "qwen_qwen3_embedding_0_6b"
    assert config._slug("nlpai-lab/KURE-v1") == "nlpai_lab_kure_v1"


def test_query_prompt_branches_on_model_name():
    assert embed.query_prompt("BAAI/bge-m3") == "" and embed.query_prompt("nlpai-lab/KURE-v1") == ""
    assert embed.query_prompt("Qwen/Qwen3-Embedding-0.6B") == "Instruct: Given a query, retrieve relevant incident tickets\nQuery: "
    assert embed.query_prompt("intfloat/multilingual-e5-large") == "query: "


def test_prefix_applies_instruction_to_queries_only(monkeypatch):
    monkeypatch.setattr(config, "EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    assert embed._prefix(["진공 저하"], "query") == [embed.QWEN3_QUERY_INSTRUCTION + "진공 저하"]
    assert embed._prefix(["설비: PECVD"], "passage") == ["설비: PECVD"]
    monkeypatch.setattr(config, "EMBED_MODEL", "BAAI/bge-m3")
    assert embed._prefix(["a"], "query") == ["a"] and embed._prefix(["a"], "passage") == ["a"]
