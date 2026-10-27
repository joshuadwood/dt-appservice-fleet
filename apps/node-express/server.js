// node-express — minimal in-memory CRUD API (Express framework)
const express = require('express');
const app = express();
app.use(express.json());

let items = [];
let nextId = 1;

app.get('/', (_req, res) => res.json({ app: 'node-express', framework: 'express', status: 'ok' }));
app.get('/health', (_req, res) => res.json({ status: 'healthy', uptime: process.uptime() }));

app.get('/items', (_req, res) => res.json(items));
app.post('/items', (req, res) => {
  const item = { id: nextId++, name: (req.body && req.body.name) || 'unnamed', createdAt: Date.now() };
  items.push(item);
  res.status(201).json(item);
});
app.get('/items/:id', (req, res) => {
  const item = items.find((i) => i.id === Number(req.params.id));
  return item ? res.json(item) : res.status(404).json({ error: 'not found' });
});
app.put('/items/:id', (req, res) => {
  const item = items.find((i) => i.id === Number(req.params.id));
  if (!item) return res.status(404).json({ error: 'not found' });
  if (req.body && req.body.name) item.name = req.body.name;
  return res.json(item);
});
app.delete('/items/:id', (req, res) => {
  const before = items.length;
  items = items.filter((i) => i.id !== Number(req.params.id));
  return res.status(items.length < before ? 204 : 404).end();
});

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`node-express listening on ${port}`));
