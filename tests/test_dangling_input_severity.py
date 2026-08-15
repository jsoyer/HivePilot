"""An absent producer is a design choice; a late producer is a bug.

The dangling-input check treated both identically. So `forage` — which
deliberately has no CTO, because it IS the pipeline without a planning phase —
was told its developer had a "dangling input", in the same words a genuine
ordering mistake would get.

The operator's correction was exact: a CTO is not mandatory, it depends on the
pipeline.

    producer absent from the pipeline entirely  -> composition choice. Say what
                                                   it implies: the stage runs
                                                   without that input.
    producer present but ordered LATER          -> real ordering fault, and the
                                                   severity already applied is
                                                   right for it.

`optional_inputs` already exists as an escape hatch, but it lives on the ROLE:
marking `technical_spec` optional there would silence it in `noxys` too, where
the CTO does run and the input genuinely should be present. The distinction has
to be per pipeline, which is exactly what the data-flow walk already knows.
"""

from __future__ import annotations

import warnings

import yaml

from hivepilot.services import config_validation

CONSUMER = {"name": "dev", "inputs": ["technical_spec"], "outputs": ["implementation"]}
PRODUCER = {"name": "cto", "inputs": [], "outputs": ["technical_spec"]}


def _run(tmp_path, stages, roles) -> list[str]:
    """Write a minimal config to disk and validate it for real.

    `validate_config` reads YAML from a directory; driving it with in-memory
    dicts would be testing a function that does not exist.
    """
    (tmp_path / "projects.yaml").write_text(yaml.safe_dump({"projects": {}}))
    (tmp_path / "roles.yaml").write_text(yaml.safe_dump({"roles": roles}))
    (tmp_path / "tasks.yaml").write_text(
        yaml.safe_dump({"tasks": {f"t-{s['name']}": {"role": s["role"]} for s in stages}})
    )
    (tmp_path / "pipelines.yaml").write_text(
        yaml.safe_dump(
            {
                "pipelines": {
                    "p": {
                        "description": "d",
                        "stages": [{"name": s["name"], "task": f"t-{s['name']}"} for s in stages],
                    }
                }
            }
        )
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config_validation.validate_config(base_dir=tmp_path)
    return [str(w.message) for w in caught]


class TestAnAbsentProducerIsNotAnError:
    def test_it_says_the_stage_runs_without_the_input(self, tmp_path):
        """forage's case. The message must describe the consequence, not label
        the config broken."""
        messages = _run(tmp_path, [{"name": "Implementation", "role": "dev"}], [CONSUMER])

        joined = " ".join(messages).lower()
        assert joined, "the operator still deserves to be told"
        assert "without" in joined
        assert "technical_spec" in joined

    def test_it_does_not_call_it_dangling(self, tmp_path):
        """'dangling input' reads as a configuration mistake. Nothing is
        dangling — the producing role simply is not in this pipeline."""
        messages = _run(tmp_path, [{"name": "Implementation", "role": "dev"}], [CONSUMER])

        assert "dangling" not in " ".join(messages).lower()


class TestALateProducerIsStillFlaggedAsOne:
    def test_a_producer_ordered_after_its_consumer_is_a_real_fault(self, tmp_path):
        """Here something IS wrong: the pipeline has the producer and runs it
        too late. That must keep the stronger wording."""
        messages = _run(
            tmp_path,
            [{"name": "Implementation", "role": "dev"}, {"name": "CTO", "role": "cto"}],
            [CONSUMER, PRODUCER],
        )

        joined = " ".join(messages).lower()
        assert "technical_spec" in joined
        assert "later" in joined or "order" in joined


class TestASatisfiedInputSaysNothing:
    def test_producer_before_consumer_is_silent(self, tmp_path):
        messages = _run(
            tmp_path,
            [{"name": "CTO", "role": "cto"}, {"name": "Implementation", "role": "dev"}],
            [CONSUMER, PRODUCER],
        )

        assert not [m for m in messages if "technical_spec" in m]
