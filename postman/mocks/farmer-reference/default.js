// DCI envelope from OpenG2P dci-search.md; record fields are the receiving
// trust contract, not a claim about the currently deployed Farmer template.
// Keys are generated in memory for each local run and never written to disk.
const http = require('http');
const crypto = require('crypto');
const source = crypto.generateKeyPairSync('rsa', {modulusLength: 2048});
let callerPublicKey;
let workflowMode = 'valid';
let workflows = new Map();
let workflowAttempts = 0;
let identityWrites = 0;
const sorted = value => Array.isArray(value) ? value.map(sorted) :
  value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, sorted(value[k])])) : value;
const canonical = value => Buffer.from(JSON.stringify(sorted(value)));
const sign = value => {
  const left = Buffer.from(JSON.stringify({alg: 'RS256', kid: 'synthetic-source'})).toString('base64url');
  const payload = canonical(value).toString('base64url');
  return `${left}..${crypto.sign('RSA-SHA256', Buffer.from(`${left}.${payload}`), source.privateKey).toString('base64url')}`;
};
const verify = (signature, payload) => {
  const [left, , right] = signature.split('.');
  return crypto.verify('RSA-SHA256', Buffer.from(`${left}.${payload.toString('base64url')}`),
    callerPublicKey, Buffer.from(right, 'base64url'));
};
http.createServer(async (req, res) => {
  // These synthetic dependencies exercise the actual native HTTP clients.
  // They do not claim to run AWE's engine or Keycloak authorization internals.
  const pathname = new URL(req.url, 'http://synthetic.invalid').pathname;
  const json = value => res.end(JSON.stringify(value));
  const bodyJson = () => typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
  res.setHeader('Content-Type', 'application/json');
  if (pathname.endsWith('/workflow-configure') && req.method === 'POST') {
    workflowMode = bodyJson().mode || 'valid';
    if (bodyJson().reset) { workflows = new Map(); workflowAttempts = 0; identityWrites = 0; }
    return json({ok:true});
  }
  if (pathname.endsWith('/workflow-stats') && req.method === 'GET') {
    return json({requests:workflows.size, attempts:workflowAttempts, identity_writes:identityWrites,
      contexts:[...workflows.values()].map(item => item.context)});
  }
  if (pathname.includes('/iam/')) {
    if (pathname.endsWith('/protocol/openid-connect/token') && req.method === 'POST') {
      return json({access_token:'synthetic-workflow-token',expires_in:300});
    }
    if (req.method !== 'GET') { identityWrites++; res.statusCode=501; return json({}); }
    if (req.headers.authorization !== 'Bearer synthetic-workflow-token') { res.statusCode=401; return json({}); }
    if (pathname.endsWith('/clients')) return json([{id:'synthetic-client'}]);
    if (pathname.endsWith('/roles/Farmer%20Reference%20Approver/users')) {
      if (workflowMode === 'iam-unavailable') { res.statusCode=503; return json({}); }
      return json(['old','foreign','unscoped','disabled'].map(name =>
        ({id:name,username:`${name}-reviewer`,enabled:name!=='disabled'})));
    }
    if (pathname.endsWith('/composite')) {
      const name = pathname.split('/users/')[1].split('/')[0];
      return json([{name:'Farmer Reference Approver'}, ...
        (name === 'old' ? [{name:'DP_old_woreda'}] : name === 'foreign' ? [{name:'DP_foreign_woreda'}] : [])]);
    }
    res.statusCode=501; return json({});
  }
  if (pathname.endsWith('/awe/v1/awe/requests') && req.method === 'POST') {
    workflowAttempts++;
    if (req.headers.authorization !== 'Bearer synthetic-workflow-token') { res.statusCode=401; return json({}); }
    const body = bodyJson();
    const context = body.context;
    if (!/^coss\.(cropsown|livestock)\.farmer-reference\.v1$/.test(body.policy_key) ||
        body.artifact_type !== 'registry.change_request' ||
        context.farmer_reference_woreda_id !== 'woreda-1' ||
        JSON.stringify(context.farmer_reference_approver_ids) !== '["old-reviewer"]' ||
        req.headers['idempotency-key'] !== `cr-${body.artifact_id}`) {
      res.statusCode=422; return json({errorCode:'SYNTHETIC-CONTRACT',message:'Unexpected workflow contract'});
    }
    if (workflowMode === 'awe-unavailable') { res.statusCode=503; return json({errorCode:'UNAVAILABLE',message:'synthetic unavailable'}); }
    const key = req.headers['idempotency-key'];
    if (!workflows.has(key)) workflows.set(key, {...body, request_id:crypto.randomUUID()});
    if (workflowMode === 'lost-response') { res.statusCode=503; return json({errorCode:'LOST',message:'synthetic lost response'}); }
    return json({request_id:workflows.get(key).request_id,status:'in_progress',current_stage_order:1,
      tasks:[{assignee:'old-reviewer',status:'open'}]});
  }
  const mode = req.url.split('?')[0].split('/').filter(Boolean).pop();
  res.setHeader('Content-Type', 'application/json');
  if (req.method === 'GET' && mode === 'public-key') {
    return res.end(JSON.stringify({public_key: source.publicKey.export({type:'spki',format:'pem'})}));
  }
  if (req.method !== 'POST') { res.statusCode = 501; return res.end('{}'); }
  let body;
  try { body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body; }
  catch { res.statusCode = 400; return res.end('{}'); }
  if (mode === 'configure') { callerPublicKey = body.public_key; return res.end('{}'); }
  if (mode === 'timeout') await new Promise(resolve => setTimeout(resolve, 1000));
  if (mode === 'html') { res.statusCode=502; res.setHeader('Content-Type','text/html'); return res.end('<html>proxy down</html>'); }
  if (['unauthorized','rate-limit','unavailable','redirect'].includes(mode)) {
    res.statusCode = {unauthorized:401,'rate-limit':429,unavailable:503,redirect:307}[mode];
    if (mode === 'redirect') res.setHeader('Location','/farmer/valid');
    return res.end('{}');
  }
  if (mode === 'invalid-json') return res.end('{');
  try {
    if (!verify(body.signature, canonical({header:body.header,message:body.message}))) throw Error();
    const criteria = body.message.search_request[0].search_criteria;
    const query = criteria.query.value.expression.query;
    const finRequest = ['fin-valid','fin-zero','fin-wrong-identity'].includes(mode);
    const expectedIdentity = finRequest ? '000000000001' : '0000000000000001';
    if ((query.functional_record_id && query.functional_record_id.$eq !== 'FR-000000000001') ||
        (!finRequest && !query.functional_record_id) || query.record_status.$eq !== 'ACTIVE' ||
        (query.foundational_id?.$eq !== expectedIdentity && query.internal_record_id?.$eq !== 'synthetic-source-person')) throw Error();
    const consentToken = criteria.authorize.consent_jws;
    const [left, middle, right] = consentToken.split('.');
    if (!crypto.verify('RSA-SHA256',Buffer.from(`${left}.${middle}`),callerPublicKey,Buffer.from(right,'base64url'))) throw Error();
    const consent = JSON.parse(Buffer.from(middle,'base64url'));
    if (consent.subject_id.value !== expectedIdentity ||
        consent.subject_id.type !== (finRequest ? 'fayda_fin' : 'fayda_fan_id') ||
        consent.data_scopes.join(',') !== 'farmer_personal_details' ||
        Date.parse(consent.validity.valid_until) <= Date.now()) throw Error();
  } catch { res.statusCode=422; return res.end(JSON.stringify({error:'invalid signed request or consent'})); }
  const record = {internal_record_id:'synthetic-source-person',functional_record_id:'FR-000000000001',
    foundational_id:'0000000000000001',record_status:'ACTIVE',last_approved_at:new Date(Date.now()-60000).toISOString(),
    first_name:'Synthetic',last_name:'Farmer',woreda_id:'synthetic-woreda',email:null,
    household_income:999999,documents:['must-not-copy'],animal_count:100};
  if (mode === 'wrong-identity') record.foundational_id='0000000000000002';
  if (mode === 'rotated') record.foundational_id='0000000000000002';
  if (mode === 'different-source-person') record.internal_record_id='another-source-person';
  if (['fin-valid','fin-zero'].includes(mode)) record.foundational_id='000000000001';
  if (mode === 'fin-wrong-identity') record.foundational_id='000000000002';
  if (mode === 'fin-is-not-uid') record.foundational_id='000000000001';
  if (mode === 'inactive') record.record_status='INACTIVE';
  if (mode === 'missing-source-id') delete record.internal_record_id;
  if (mode === 'missing-approval') delete record.last_approved_at;
  if (mode === 'omitted-email') delete record.email;
  if (mode === 'current-template') {
    for (const key of Object.keys(record)) delete record[key];
    record.farmer_personal_details={member_identifier:[{identifier_type:'UIN',identifier_value:'000000000001'}],
      demographic_info:{name:{given_name:'Synthetic',surname:'Farmer'}}};
  }
  const header = {version:'1.0.0',message_id:body.header.message_id,message_ts:new Date().toISOString(),action:'search',
    status:'succ',sender_id:body.header.receiver_id,receiver_id:body.header.sender_id,
    meta:{signature_validation:'enabled',consent_enforcement:'enabled'}};
  const item = {reference_id:body.message.search_request[0].reference_id,status:'succ',data:{reg_records:[record]}};
  const message = {transaction_id:body.message.transaction_id,search_response:[item]};
  if (mode === 'multiple') item.data.reg_records.push(record);
  if (mode === 'zero' || mode === 'fin-zero') item.data.reg_records=[];
  if (mode === 'wrong-correlation') message.transaction_id='other-transaction';
  if (mode === 'wrong-reference') item.reference_id='other-reference';
  if (mode === 'wrong-sender') header.sender_id='other-registry';
  if (mode === 'consent-disabled') header.meta.consent_enforcement='disabled';
  if (mode === 'stale') header.message_ts=new Date(Date.now()-3600000).toISOString();
  const envelope = {signature:sign({header,message}),header,message};
  if (mode === 'unsigned') envelope.signature='signature_validation_disabled';
  if (mode === 'tampered') record.first_name='tampered';
  res.end(JSON.stringify(envelope));
}).listen(process.env.PORT || 49183);
