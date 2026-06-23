"""DeepEval-based evaluation harness for the BotCircuits agent.

See evals/README.md. Import order matters: `instrument` patches Agent /
ToolRegistry to emit DeepEval spans and must run before an Agent is built.
"""
