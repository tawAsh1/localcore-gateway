"""Pluggable local AWS Lambda backend.

Two interchangeable backends behind one ``LambdaInvoker`` interface:

* ``native`` (default) -- an in-process emulator. No Docker. Instant start,
  hot reload. Faithful ``event`` / ``context`` contract (including
  ``client_context.custom``), Lambda error envelope, CloudWatch-style
  ``START`` / ``END`` / ``REPORT`` log lines. Soft timeout (documented
  caveat: a running handler thread cannot be hard-killed in-process).

* ``sam`` -- drives a running ``sam local start-lambda`` endpoint. Uses the
  real AWS Lambda Linux runtime image, so it is byte-for-byte the production
  runtime. Requires Docker + the AWS SAM CLI. Hard timeout / isolation.

Pick per target/config: ``native`` for the fast dev loop, ``sam`` to verify
fidelity before deploying to real AWS.
"""

from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker, make_invoker

__all__ = ["InvokeResult", "LambdaInvoker", "make_invoker"]
