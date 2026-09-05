import http from 'node:http';
import { WebSocketServer } from 'ws';

const prefix = '/flowweave/api/v1';
const authenticated = (request) => request.headers.cookie === 'flowweave_session=session-secret';
const user = { id: 'user-1', username: 'flowweave', role: 'SUPER_ADMIN', is_super_admin: true };
const websocket = new WebSocketServer({ noServer: true });

const server = http.createServer((request, response) => {
  if (request.method === 'POST' && request.url === `${prefix}/auth/login`) {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      const credentials = JSON.parse(body);
      if (credentials.username !== 'flowweave' || credentials.password !== 'correct-password') {
        response.writeHead(401, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ error: { code: 'AUTHENTICATION_FAILED' } }));
        return;
      }
      response.writeHead(200, {
        'Content-Type': 'application/json',
        'Set-Cookie': 'flowweave_session=session-secret; Max-Age=43200; HttpOnly; Path=/; SameSite=Lax',
      });
      response.end(JSON.stringify(user));
    });
    return;
  }
  if (request.method === 'POST' && request.url === `${prefix}/auth/logout`) {
    response.writeHead(authenticated(request) ? 204 : 401);
    response.end();
    return;
  }
  if (request.method === 'GET' && request.url === `${prefix}/auth/me`) {
    response.writeHead(authenticated(request) ? 200 : 401, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify(authenticated(request) ? user : { error: { code: 'AUTHENTICATION_REQUIRED' } }));
    return;
  }
  if (request.method === 'GET' && request.url === `${prefix}/flows` && authenticated(request)) {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(JSON.stringify([{ id: 'flow-1' }]));
    return;
  }
  response.writeHead(401, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify({ error: { code: 'AUTHENTICATION_REQUIRED' } }));
});

server.on('upgrade', (request, socket, head) => {
  if (request.url !== `${prefix}/authenticated-stream` || !authenticated(request)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }
  websocket.handleUpgrade(request, socket, head, (connection) => {
    connection.send(JSON.stringify({ event: 'authenticated-websocket' }));
  });
});

server.listen(0, '127.0.0.1', () => {
  const address = server.address();
  process.stdout.write(`${address.port}\n`);
});

process.on('SIGTERM', () => server.close());
