// 差分对拍的参照实现：AES-256-GCM / AES-256-ECB / HKDF-SHA256，全部来自 Node 内置 crypto。
// 用法: node _node_crypto_ref.js <cases.json> <out.json>
// 被 tests/test_crypto.py 调用；没有 Node 时那边会自动跳过对拍。
const crypto = require('crypto');
const fs = require('fs');

const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = cases.map((c) => {
  if (c.kind === 'gcm') {
    const cipher = crypto.createCipheriv(
      'aes-256-gcm', Buffer.from(c.key, 'hex'), Buffer.from(c.nonce, 'hex'));
    if (c.aad) cipher.setAAD(Buffer.from(c.aad, 'hex'));
    const ct = Buffer.concat([cipher.update(Buffer.from(c.pt, 'hex')), cipher.final()]);
    return ct.toString('hex') + cipher.getAuthTag().toString('hex');
  }
  if (c.kind === 'ecb') {
    const cipher = crypto.createCipheriv('aes-256-ecb', Buffer.from(c.key, 'hex'), null);
    cipher.setAutoPadding(false);
    return Buffer.concat([cipher.update(Buffer.from(c.pt, 'hex')), cipher.final()]).toString('hex');
  }
  if (c.kind === 'hkdf') {
    return Buffer.from(crypto.hkdfSync('sha256', Buffer.from(c.ikm, 'hex'),
      Buffer.from(c.salt, 'hex'), Buffer.from(c.info, 'hex'), c.length)).toString('hex');
  }
  throw new Error('unknown kind ' + c.kind);
});
fs.writeFileSync(process.argv[3], JSON.stringify(out));
