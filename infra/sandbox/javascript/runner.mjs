import vm from 'node:vm';

let input = '';
for await (const chunk of process.stdin) input += chunk;
try {
  const payload = JSON.parse(input);
  const code = String(payload.code ?? '');
  if (!code || Buffer.byteLength(code) > 32768) throw new Error('JavaScript gate code is empty or too large');
  const context = structuredClone(payload.context ?? {});
  const source = `(function(context){'use strict';${code}})(context)`;
  const result = vm.runInNewContext(source, { context }, {
    timeout: 300000,
    contextCodeGeneration: { strings: false, wasm: false },
  });
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  process.stdout.write(JSON.stringify({ runner_error: String(error?.message ?? error) }));
  process.exitCode = 2;
}
