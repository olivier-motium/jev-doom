"""Keyless local choice policy; no model or provider call is used."""

from jevdoom.core.tactics import fixed_select


class LocalDecider:
    billed = False

    def ask(self, job):
        pick = fixed_select(job["state"]["tactic_candidates"])
        selected_option = next(option for option in job["state"]["tactic_candidates"] if option["id"] == pick)
        fire = "allow" if selected_option["controls"]["trigger"] == "fire" else "hold"
        answers = {}
        for name, question in job["questions"].items():
            selected = {"dodge": "carry_on", "fire": fire}.get(name, pick)
            answers[name] = {
                "type": "choice",
                "choice": selected,
                "probabilities": {
                    option: float(option == selected) for option in question["criteria"]
                },
            }
        return {
            "ok": True,
            "answers": answers,
            "cost_usd": 0.0,
            "elapsed_ms": 0.0,
            "model": "Local rule, no model",
        }
