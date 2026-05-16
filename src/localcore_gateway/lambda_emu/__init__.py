"""Pluggable local AWS Lambda backend.

Two interchangeable backends behind one ``LambdaInvoker`` interface:

* ``native`` (default) -- one subprocess worker per target (one warm Lambda
  execution environment). No Docker. Real ``sys.path`` / ``sys.modules``
  isolation, so same-named handler modules across targets (the monorepo
  case) never collide. Faithful ``event`` / ``context`` (including
  ``client_context.custom``), Lambda error envelope, CloudWatch-style
  ``START`` / ``END`` / ``REPORT`` lines, hot reload, **hard** timeout.

* ``sam`` -- drives a running ``sam local start-lambda`` endpoint. Uses the
  real AWS Lambda Linux runtime image, so it is byte-for-byte the production
  runtime. Requires Docker + the AWS SAM CLI.

Pick per target/config: ``native`` for the fast dev loop, ``sam`` to verify
fidelity before deploying to real AWS.
"""

from localcore_gateway.lambda_emu.base import InvokeResult, LambdaInvoker, make_invoker

__all__ = ["InvokeResult", "LambdaInvoker", "make_invoker"]
