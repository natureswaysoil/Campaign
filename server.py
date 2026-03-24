#!/usr/bin/env python3
"""
FastAPI server for Cloud Run
Provides HTTP endpoint to trigger campaign optimization
"""

import os
from fastapi import FastAPI, BackgroundTasks

app = FastAPI()

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": "healthy", "service": "campaign-optimizer"}

@app.post("/optimize")
async def optimize(background_tasks: BackgroundTasks):
    """Trigger campaign optimization"""
    # Import here to avoid slow startup
    from run_optimizer import main as run_optimizer
    
    # Run in background so request returns immediately
    background_tasks.add_task(run_optimizer)
    return {"status": "started", "message": "Campaign optimization triggered"}

@app.get("/health")
async def health():
    """Health check for Cloud Run"""
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    print(f"Starting server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
