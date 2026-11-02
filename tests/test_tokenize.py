"""BM25 토크나이저: 코드 토큰 보존, 태그 필터, 사용자 사전."""
from rag.tokenize import tokenize, user_words


def test_codes_survive_even_glued_to_korean():
    toks = tokenize("PECVD-03에서 E-1101 알람 뜨고 INC-2026-0002 참조")
    assert {"pecvd-03", "e-1101", "inc-2026-0002"} <= set(toks)
    assert "-" not in toks and "1101" not in toks            # 하이픈 조각이 따로 남지 않는다


def test_particles_and_verbs_dropped_nouns_kept():
    toks = tokenize("펌프다운이 평소보다 오래 걸려요")
    assert "펌프다운" in toks and not {"이", "걸리", "어요"} & set(toks)


def test_catalog_user_dict_keeps_domain_terms_whole():
    assert {"베인", "O-ring", "드라이펌프"} <= set(user_words())
    toks = tokenize("드라이펌프 베인 마모, O-ring 교체")
    assert {"드라이펌프", "베인", "o-ring"} <= set(toks)
