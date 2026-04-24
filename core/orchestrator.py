@app.post("/orchestrate")
def orchestrate(req: OrchestrateRequest):
    return run_orchestrator(req)
