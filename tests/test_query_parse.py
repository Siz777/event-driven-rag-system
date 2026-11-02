from rag.query_parse import parse_query


def test_code_variants_normalize():
    for q in ["E-1101 떴어요", "e1101 전에 뜬 적?", "E1101알람", "e-1101"]:
        assert parse_query(q).alarm_codes == ["E-1101"], q
    assert parse_query("E-11010 아님, E-110 아님").alarm_codes == []


def test_equipment_id_and_alias():
    f = parse_query("pecvd03이랑 CVD-7 둘 다")
    assert f.equipment_ids == ["PECVD-03", "PECVD-07"] and f.equipment_types == ["PECVD"] and f.type_filter == ["PECVD"]


def test_type_keywords_and_ambiguity():
    assert parse_query("식각기 ESC 전압 이상").type_filter == ["ETCH"]
    assert parse_query("연마율이 떨어져요").type_filter == ["CMP"]
    f = parse_query("포토 트랙이랑 CMP 둘 다 이상")
    assert f.ambiguous_types and f.type_filter == [] and set(f.equipment_types) == {"PHOTO", "CMP"}
    assert parse_query("펌프다운이 오래 걸려요").is_empty()
