// node-fastify — minimal in-memory CRUD API (Fastify framework)
const Fastify = require('fastify');
const fastify = Fastify({ logger: true });

let items = [];
let nextId = 1;

fastify.get('/', async () => ({ app: 'node-fastify', framework: 'fastify', status: 'ok' }));
fastify.get('/health', async () => ({ status: 'healthy', uptime: process.uptime() }));

fastify.get('/items', async () => items);
fastify.post('/items', async (req, reply) => {
  const item = { id: nextId++, name: (req.body && req.body.name) || 'unnamed', createdAt: Date.now() };
  items.push(item);
  reply.code(201);
  return item;
});
fastify.get('/items/:id', async (req, reply) => {
  const item = items.find((i) => i.id === Number(req.params.id));
  if (!item) { reply.code(404); return { error: 'not found' }; }
  return item;
});
fastify.put('/items/:id', async (req, reply) => {
  const item = items.find((i) => i.id === Number(req.params.id));
  if (!item) { reply.code(404); return { error: 'not found' }; }
  if (req.body && req.body.name) item.name = req.body.name;
  return item;
});
fastify.delete('/items/:id', async (req, reply) => {
  const before = items.length;
  items = items.filter((i) => i.id !== Number(req.params.id));
  reply.code(items.length < before ? 204 : 404);
  return null;
});

const port = process.env.PORT || 3000;
fastify.listen({ port, host: '0.0.0.0' }).catch((err) => { fastify.log.error(err); process.exit(1); });
