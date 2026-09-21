import {experimental_evaluate as evaluate} from 'ai';
import readline from 'node:readline';
const DIRECT_ENDPOINT='https://api.typesafe.ai/v1/systemone';
const PRICE={input_usd_per_token:0.000000042,output_usd_per_token:0,source:'https://typesafe.ai/blog/introducing-system-one-models-and-jev',published:'2026-09-15',verified:'2026-09-18',label:'Public-rate estimate; not an account-specific charge'};
const redact=s=>{let out=String(s);for(const key of [process.env.AI_GATEWAY_API_KEY,process.env.TYPESAFE_API_KEY])if(key)out=out.replaceAll(key,'[REDACTED]');return out;};
// Both credentials remain in this server-side process. Never return headers.
for await (const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})) {
 const start=performance.now();let req,signal,provider='gateway',endpoint='https://ai-gateway.vercel.sh/v4/ai/evaluation-model',sent_at=new Date().toISOString();
 try {
  req=JSON.parse(line);provider=req.provider??'gateway';
  if(Buffer.byteLength(line)>48000||Object.keys(req.questions).length>12)throw Error('Request bound exceeded');
  signal=AbortSignal.timeout(20000);
  if(provider==='typesafe'){
   endpoint=DIRECT_ENDPOINT;
   if(!process.env.TYPESAFE_API_KEY)throw Error('TYPESAFE_API_KEY is unavailable');
   const response=await fetch(endpoint,{method:'POST',headers:{'Authorization':'Bearer '+process.env.TYPESAFE_API_KEY,'Content-Type':'application/json'},body:JSON.stringify({state:req.state,model:'jev-latest',questions:req.questions}),signal});
   const body=await response.text();let r;try{r=JSON.parse(body);}catch{throw Object.assign(Error('TypeSafe returned non-JSON response'),{statusCode:response.status});}
   if(!response.ok)throw Object.assign(Error(redact(JSON.stringify(r)).slice(0,500)),{statusCode:response.status});
   const explicitCost=r.cost_usd??r.usage?.cost_usd??null;
   const cost=explicitCost!==null&&Number.isFinite(Number(explicitCost))&&Number(explicitCost)>=0?Number(explicitCost):null;
   const input=r.usage?.input_tokens,output=r.usage?.output_tokens;
   console.log(JSON.stringify({ok:true,provider,endpoint,http_status:response.status,sent_at,received_at:new Date().toISOString(),answers:r.answers,rounding:r.rounding??null,usage:input!=null?{inputTokens:input,outputTokens:output??null,totalTokens:output!=null?input+output:null}:null,raw_response:r,confidence:Object.fromEntries(Object.entries(r.answers??{}).map(([k,v])=>[k,v.confidence??null])),cost_usd:cost,estimated_cost_usd:Number.isFinite(input)?input*PRICE.input_usd_per_token:null,estimate_basis:PRICE,model:r.model??'jev-latest',elapsed_ms:performance.now()-start}));
  }else if(provider==='gateway'){
   if(!process.env.AI_GATEWAY_API_KEY)throw Error('AI_GATEWAY_API_KEY is unavailable');
   const r=await evaluate({model:'typesafe-ai/jev',state:req.state,questions:req.questions,maxRetries:0,abortSignal:signal});
   const cost=r.providerMetadata?.gateway?.cost;
   console.log(JSON.stringify({ok:true,provider,endpoint,sent_at,received_at:new Date().toISOString(),answers:r.answers,rounding:r.rounding??null,usage:r.usage??null,confidence:r.providerMetadata?.typesafe?.confidence??null,cost_usd:cost!=null&&Number.isFinite(Number(cost))?Number(cost):null,model:r.response?.modelId??'typesafe-ai/jev',elapsed_ms:performance.now()-start}));
  }else throw Error('Unknown provider');
 }catch(e){console.log(JSON.stringify({ok:false,provider,endpoint,sent_at,received_at:new Date().toISOString(),error:redact(e.message).slice(0,500),invalid_answers:e.name==='AI_InvalidResponseDataError'&&e.data?JSON.parse(redact(JSON.stringify(e.data))):null,error_name:e.name??null,cause_name:e.cause?.name??null,client_aborted:signal?.aborted??false,abort_reason_name:signal?.reason?.name??null,client_timeout_ms:20000,status:e.statusCode??null,elapsed_ms:performance.now()-start}));}
}
