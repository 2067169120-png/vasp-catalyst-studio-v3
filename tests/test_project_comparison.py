from vcstudio.project import comparison


def _summary(name, values, method="verified"):
    rows = []
    for species, entries in values.items():
        for index, value in enumerate(entries):
            rows.append({
                "name": f"{name}_{species}_{index}",
                "species": species,
                "state": "DONE",
                "delta_e": value,
                "method_check": {"status": method},
            })
    return {
        "rows": rows,
        "method_consistency": {"status": method, "issues": [], "warnings": []},
    }


def _fed(offset=0.0, corrected=False):
    return {
        "steps": [
            {"label": "S8*", "G": 0.0 + offset},
            {"label": "Li2S4*", "G": -0.7 + offset},
            {"label": "Li2S*", "G": -1.1 + offset},
        ],
        "pds_index": 1,
        "u_l": 1.23,
        "thermo_corrected": corrected,
        "reference": "Li/Li+",
    }


def test_canonical_species_and_stable_config_deadband():
    assert comparison.canonical_species("*LiS₂") == "LiS2"
    assert comparison.canonical_species("ads_Li₂S₃_bridge") == "Li2S3"
    assert comparison.canonical_species("ads_Li2S10_top") == "Li2S10"
    assert comparison.canonical_species("ads_Li2S10_top") != "Li2S"
    summary = _summary("Fe", {"Li₂S₈": [-1.04, -0.98, -0.70], "Li2S": [-2.81]})
    rows = comparison.stable_species_rows(summary, deadband_eV=0.15)
    assert [row["species"] for row in rows] == ["Li2S8", "Li2S"]
    assert rows[0]["delta_e"] == -1.04
    assert [row["delta_e"] for row in rows[0]["co_minima"]] == [-0.98]
    assert rows[0]["near_degenerate"] is True


def test_snapshot_preserves_missing_and_deduplicates_paths():
    project = {"name": "Fe", "root": "/data/a", "project_uuid": "abc"}
    item = {"path": "/data/a/project.yaml", "project": project,
            "summary": _summary("Fe", {"Li2S8": [-1.0]}), "fed": _fed()}
    snapshot = comparison.build_comparison_snapshot([
        item, dict(item), {"path": "/moved/project.yaml", "project": None},
    ])
    assert snapshot["selected_count"] == 2
    moved = snapshot["projects"][1]
    assert moved["status"] == "blocked"
    assert "已被移动" in moved["block_reasons"][0]


def test_snapshot_uses_canonical_species_matrix_and_authoritative_ul():
    items = []
    for index, name in enumerate(("Fe", "Co")):
        items.append({
            "path": f"/p/{index}",
            "project": {
                "name": name, "root": f"/p/{index}",
                "comparison_method_fingerprint": "same",
            },
            "summary": _summary(name, {
                "Li2S8": [-1.0 - index * 0.1, -0.8],
                "Li2S": [-2.8 - index * 0.1],
            }),
            "fed": _fed(index * 0.1),
        })
    snapshot = comparison.build_comparison_snapshot(items)
    assert snapshot["can_plot"] is True
    assert snapshot["can_final_report"] is True
    assert snapshot["adsorption_matrix"]["cols"] == ["Li2S8", "Li2S"]
    assert snapshot["adsorption_matrix"]["values"][0] == [-1.0, -2.8]
    assert snapshot["ladder"]["paths"][0]["u_l"] == 1.23


def test_duplicate_project_names_get_unique_display_names():
    items = []
    for parent in ("a", "b"):
        items.append({
            "path": f"/{parent}/same/project.yaml",
            "project": {
                "name": "Fe", "root": f"/{parent}/same",
                "project_uuid": parent * 12,
                "comparison_method_fingerprint": "same",
            },
            "summary": _summary("Fe", {"Li2S": [-2.0]}),
            "fed": _fed(),
        })
    snapshot = comparison.build_comparison_snapshot(items)
    names = [project["display_name"] for project in snapshot["projects"]]
    assert len(set(names)) == 2
    assert all(name.startswith("Fe · ") for name in names)


def test_mixed_path_or_energy_basis_blocks_overlay():
    first = {
        "path": "/a", "project": {
            "name": "A", "comparison_method_fingerprint": "same"},
        "summary": _summary("A", {"Li2S": [-2.0]}), "fed": _fed(corrected=False),
    }
    second = {
        "path": "/b", "project": {
            "name": "B", "comparison_method_fingerprint": "same"},
        "summary": _summary("B", {"Li2S": [-2.1]}), "fed": _fed(corrected=True),
    }
    snapshot = comparison.build_comparison_snapshot([first, second])
    assert snapshot["can_plot"] is False
    assert snapshot["comparison_gate"]["status"] == "incompatible"
    assert any("口径" in reason for reason in snapshot["comparison_gate"]["blocking"])


def test_missing_cross_project_method_fingerprint_allows_diagnostic_not_final():
    items = [
        {"path": "/a", "project": {"name": "A"},
         "summary": _summary("A", {"Li2S": [-2.0]}), "fed": _fed()},
        {"path": "/b", "project": {"name": "B"},
         "summary": _summary("B", {"Li2S": [-2.1]}), "fed": _fed()},
    ]
    snapshot = comparison.build_comparison_snapshot(items)
    assert snapshot["can_plot"] is True
    assert snapshot["comparison_gate"]["status"] == "unverified"
    assert snapshot["can_final_report"] is False


def test_generic_job_fingerprint_differences_do_not_create_false_spin_conflict():
    items = [
        {"path": "/a", "project": {"name": "A", "method_fingerprint": "ispin-1"},
         "summary": _summary("A", {"Li2S": [-2.0]}), "fed": _fed()},
        {"path": "/b", "project": {"name": "B", "method_fingerprint": "ispin-2"},
         "summary": _summary("B", {"Li2S": [-2.1]}), "fed": _fed()},
    ]
    snapshot = comparison.build_comparison_snapshot(items)
    assert snapshot["can_plot"] is True
    assert snapshot["comparison_gate"]["status"] == "unverified"
    assert not any(
        "方法指纹不一致" in reason
        for reason in snapshot["comparison_gate"]["blocking"]
    )


def test_explicit_cross_project_method_conflict_blocks_overlay():
    items = [
        {"path": "/a",
         "project": {"name": "A", "comparison_method_fingerprint": "pbe"},
         "summary": _summary("A", {"Li2S": [-2.0]}), "fed": _fed()},
        {"path": "/b",
         "project": {"name": "B", "comparison_method_fingerprint": "hse"},
         "summary": _summary("B", {"Li2S": [-2.1]}), "fed": _fed()},
    ]
    snapshot = comparison.build_comparison_snapshot(items)
    assert snapshot["can_plot"] is False
    assert snapshot["comparison_gate"]["status"] == "incompatible"
    assert any(
        "方法指纹不一致" in reason
        for reason in snapshot["comparison_gate"]["blocking"]
    )


def test_final_comparison_requires_a_ladder_for_every_ready_project():
    items = [
        {
            "path": f"/{name.lower()}",
            "project": {
                "name": name,
                "comparison_method_fingerprint": "same-protocol",
            },
            "summary": _summary(name, {"Li2S": [value]}),
            "fed": None,
            "fed_reason": "缺少配平反应路径",
        }
        for name, value in (("A", -2.0), ("B", -2.1))
    ]

    snapshot = comparison.build_comparison_snapshot(items)

    assert snapshot["ready_count"] == 2
    assert snapshot["ladder_ready_count"] == 0
    assert snapshot["can_plot"] is False
    assert snapshot["can_final_report"] is False
    assert snapshot["comparison_gate"]["status"] == "unverified"
    assert any("缺少可用自由能路径" in warning
               for warning in snapshot["comparison_gate"]["warnings"])


def test_numeric_slab_difference_without_adsorbate_reference_is_blocked():
    summary = _summary("A", {"Li2S": [-2.0]})
    summary.update({"has_ref": False, "reference_mode": "none"})
    summary["rows"][0]["reference_valid"] = False

    snapshot = comparison.build_comparison_snapshot([{
        "path": "/a",
        "project": {
            "name": "A",
            "comparison_method_fingerprint": "same-protocol",
        },
        "summary": summary,
        "fed": _fed(),
    }])

    project = snapshot["projects"][0]
    assert project["status"] == "blocked"
    assert project["species"] == []
    assert snapshot["adsorption_matrix"]["values"] == []
    assert any("参考态" in reason for reason in project["block_reasons"])


def test_actual_method_evidence_compares_common_elements_not_unique_catalysts():
    def _item(name, catalyst, catalyst_paw, *, sulfur_paw="PAW_PBE S"):
        summary = _summary(name, {"Li2S": [-2.0]})
        summary["comparison_method_evidence"] = {
            "status": "verified",
            "potcar_ids": {
                "Li": "PAW_PBE Li_sv",
                "S": sulfur_paw,
                catalyst: catalyst_paw,
            },
            "u_by_element": {
                "Li": {"enabled": False},
                "S": {"enabled": False},
                catalyst: {"enabled": False},
            },
        }
        return {
            "path": f"/{name.lower()}",
            "project": {
                "name": name,
                "comparison_method_fingerprint": "same-protocol",
            },
            "summary": summary,
            "fed": _fed(),
        }

    compatible = comparison.build_comparison_snapshot([
        _item("Fe", "Fe", "PAW_PBE Fe_pv"),
        _item("Co", "Co", "PAW_PBE Co"),
    ])
    assert compatible["comparison_gate"]["status"] == "verified"
    assert compatible["can_final_report"] is True

    incompatible = comparison.build_comparison_snapshot([
        _item("Fe", "Fe", "PAW_PBE Fe_pv"),
        _item("Co", "Co", "PAW_PBE Co", sulfur_paw="PAW_PBE S_h"),
    ])
    assert incompatible["comparison_gate"]["status"] == "incompatible"
    assert incompatible["can_plot"] is False
    assert any("共同元素 POTCAR 不一致" in reason
               for reason in incompatible["comparison_gate"]["blocking"])
