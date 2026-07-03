from __future__ import annotations

from typing import Any, Mapping


class TemplateMapping(Mapping[str, Any]):
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        if key not in self._data:
            raise KeyError(f"Unknown template variable: {key}")
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def render_template(template: str, values: dict[str, Any]) -> str:
    try:
        return template.format_map(TemplateMapping(values))
    except KeyError as exc:  # pragma: no cover
        missing = exc.args[0]
        raise ValueError(f"Template references unknown variable '{missing}'") from exc
