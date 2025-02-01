import runpod

from translate import handler

# Start Runpod serverless handler
runpod.serverless.start({"handler": handler})
