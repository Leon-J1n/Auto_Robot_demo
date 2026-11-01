"""Dispatch a validated request to the physical contact controller."""

class CircularPullPush:
    def plan(self, request, context, **kwargs):
        return context["pull_push_controller"].plan(request, **kwargs)

    def execute(self, request, context):
        return context["pull_push_controller"].execute(request)

    def execute_prepared(self, request, context, prepared):
        return context["pull_push_controller"].execute_prepared(request, prepared)
