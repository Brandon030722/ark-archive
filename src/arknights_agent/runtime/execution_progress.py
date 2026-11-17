"""Resource scheduling from observed progress, never user-language rules."""
from dataclasses import dataclass, field


@dataclass
class ExecutionProgress:
    maximum: int
    allocated: int
    extension: int
    reserve: int
    repeat_limit: int
    generation: int = 0
    last_extension_generation: int = -1
    repeats: dict = field(default_factory=dict)
    evidence: set = field(default_factory=set)
    plans: set = field(default_factory=set)
    proposals: set = field(default_factory=set)

    def allocate(self, used):
        """Extend before discovery would eat the revision/final-review reserve."""
        if (self.allocated - used <= self.reserve + 2 and self.allocated < self.maximum
                and self.generation > self.last_extension_generation):
            self.allocated = min(self.maximum, self.allocated + self.extension)
            self.last_extension_generation = self.generation
        return self.allocated - used

    def observe(self, kind, identity):
        target = {"evidence": self.evidence, "plan": self.plans, "proposal": self.proposals}[kind]
        if identity not in target:
            target.add(identity)
            self.generation += 1
            return True
        return False

    def repeat(self, action):
        # A formerly used action may be useful after a new source/plan/proposal.
        # Within one unchanged generation, repeated identical actions are idle.
        key = (action, self.generation)
        self.repeats[key] = self.repeats.get(key, 0) + 1
        return self.repeats[key] >= self.repeat_limit

    def audit(self):
        return {"allocated_model_calls": self.allocated, "maximum_model_calls": self.maximum,
                "progress_generation": self.generation, "evidence_versions": len(self.evidence),
                "plan_versions": len(self.plans), "proposal_versions": len(self.proposals),
                "revision_and_final_review_reserve": self.reserve}
