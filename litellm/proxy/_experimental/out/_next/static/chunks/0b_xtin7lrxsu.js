(globalThis.TURBOPACK||(globalThis.TURBOPACK=[])).push(["object"==typeof document?document.currentScript:void 0,972520,e=>{"use strict";let r=(0,e.i(475254).default)("arrow-right",[["path",{d:"M5 12h14",key:"1ays0h"}],["path",{d:"m12 5 7 7-7 7",key:"xquz4c"}]]);e.s(["ArrowRight",0,r],972520)},411929,e=>{"use strict";var r=e.i(843476),t=e.i(154626),a=e.i(271645),s=e.i(972520),o=e.i(174886),i=e.i(519455),d=e.i(515288),n=e.i(624687),l=e.i(571303),c=e.i(602869),m=e.i(417385);let u=({accessToken:e})=>{let[u,p]=(0,a.useState)(`{
  "model": "openai/gpt-4o",
  "messages": [
    {
      "role": "system",
      "content": "You are a helpful assistant."
    },
    {
      "role": "user",
      "content": "Explain quantum computing in simple terms"
    }
  ],
  "temperature": 0.7,
  "max_tokens": 500,
  "stream": true
}`),[f,x]=(0,a.useState)(""),[h,g]=(0,a.useState)(!1),j=async()=>{g(!0);try{let s;try{s=JSON.parse(u)}catch(e){m.toast.fromError("Invalid JSON in request body"),g(!1);return}let o={call_type:"completion",request_body:s};if(!e){m.toast.fromError("No access token found"),g(!1);return}let i=await (0,c.transformRequestCall)(e,o);if(i.raw_request_api_base&&i.raw_request_body){var r,t,a;let e,s,o=(r=i.raw_request_api_base,t=i.raw_request_body,a=i.raw_request_headers||{},e=JSON.stringify(t,null,2).split("\n").map(e=>`  ${e}`).join("\n"),s=Object.entries(a).map(([e,r])=>`-H '${e}: ${r}'`).join(" \\\n  "),`curl -X POST \\
  ${r} \\
  ${s?`${s} \\
  `:""}-H 'Content-Type: application/json' \\
  -d '{
${e}
  }'`);x(o),m.toast.success("Request transformed successfully")}else{let e="string"==typeof i?i:JSON.stringify(i);x(e),m.toast.info("Transformed request received in unexpected format")}}catch(e){console.error("Error transforming request:",e),m.toast.fromError("Failed to transform request")}finally{g(!1)}};return(0,r.jsxs)("div",{className:"p-2",children:[(0,r.jsx)("h1",{className:"text-lg font-medium text-foreground",children:"Playground"}),(0,r.jsx)("p",{className:"text-sm text-muted-foreground",children:"See how Iagami transforms your request for the specified provider."}),(0,r.jsxs)("div",{className:"mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2",children:[(0,r.jsxs)(d.Card,{children:[(0,r.jsxs)(d.CardHeader,{children:[(0,r.jsx)(d.CardTitle,{className:"text-2xl font-bold",children:"Original Request"}),(0,r.jsx)(d.CardDescription,{children:"The request you would send to Iagami /chat/completions endpoint."})]}),(0,r.jsx)(d.CardContent,{children:(0,r.jsx)(n.Textarea,{className:"h-72 resize-none p-4 font-mono text-sm field-sizing-fixed",value:u,onChange:e=>p(e.target.value),onKeyDown:e=>{(e.metaKey||e.ctrlKey)&&"Enter"===e.key&&(e.preventDefault(),j())},placeholder:"Press Cmd/Ctrl + Enter to transform"})}),(0,r.jsx)(d.CardFooter,{className:"justify-end",children:(0,r.jsxs)(i.Button,{onClick:j,disabled:h,children:[(0,r.jsx)("span",{children:"Transform"}),h?(0,r.jsx)(l.UiLoadingSpinner,{className:"size-4"}):(0,r.jsx)(s.ArrowRight,{})]})})]}),(0,r.jsxs)(d.Card,{children:[(0,r.jsxs)(d.CardHeader,{children:[(0,r.jsx)(d.CardTitle,{className:"text-2xl font-bold",children:"Transformed Request"}),(0,r.jsx)(d.CardDescription,{children:"How Iagami transforms your request for the specified provider."}),(0,r.jsx)("p",{className:"mt-2 text-xs text-muted-foreground",children:"Note: Sensitive headers are not shown."})]}),(0,r.jsx)(d.CardContent,{children:(0,r.jsxs)("div",{className:"relative rounded-md bg-muted",children:[(0,r.jsx)("pre",{className:"h-72 overflow-auto p-4 font-mono text-sm",children:f||`curl -X POST \\
  https://api.openai.com/v1/chat/completions \\
  -H 'Authorization: Bearer sk-xxx' \\
  -H 'Content-Type: application/json' \\
  -d '{
  "model": "gpt-4",
  "messages": [
    {
      "role": "system",
      "content": "You are a helpful assistant."
    }
  ],
  "temperature": 0.7
  }'`}),(0,r.jsx)(i.Button,{variant:"ghost",size:"icon-sm","aria-label":"Copy to clipboard",className:"absolute top-2 right-2",onClick:()=>{navigator.clipboard.writeText(f||""),m.toast.success("Copied to clipboard")},children:(0,r.jsx)(o.Copy,{})})]})})]})]}),(0,r.jsx)("div",{className:"mt-4 text-right",children:(0,r.jsxs)("p",{className:"text-sm text-muted-foreground",children:["Found an error? File an issue"," ",(0,r.jsx)("a",{className:"underline underline-offset-4",href:(0,t.brandSupportUrl)("https://github.com/BerriAI/litellm/issues"),target:"_blank",rel:"noopener noreferrer",children:"here"}),"."]})})]})};var p=e.i(135214);e.s(["default",0,function(){let{accessToken:e}=(0,p.default)();return(0,r.jsx)(u,{accessToken:e})}],411929)},515288,e=>{"use strict";var r=e.i(843476),t=e.i(271645),a=e.i(196631);let s=t.forwardRef(({className:e,size:t="default",...s},o)=>(0,r.jsx)("div",{ref:o,"data-slot":"card","data-size":t,className:(0,a.cn)("group/card flex flex-col gap-(--card-spacing) rounded-xl bg-card py-(--card-spacing) text-sm text-card-foreground shadow-xs ring-1 ring-foreground/10 [--card-spacing:--spacing(6)] has-[>img:first-child]:pt-0 data-[size=sm]:[--card-spacing:--spacing(4)] *:[img:first-child]:rounded-t-xl *:[img:last-child]:rounded-b-xl",e),...s}));s.displayName="Card";let o=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-header",className:(0,a.cn)("group/card-header @container/card-header grid auto-rows-min items-start gap-1 rounded-t-xl px-(--card-spacing) has-data-[slot=card-action]:grid-cols-[1fr_auto] has-data-[slot=card-description]:grid-rows-[auto_auto] [.border-b]:pb-(--card-spacing)",e),...t}));o.displayName="CardHeader";let i=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-title",className:(0,a.cn)("text-base leading-normal font-medium group-data-[size=sm]/card:text-sm",e),...t}));i.displayName="CardTitle";let d=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-description",className:(0,a.cn)("text-sm text-muted-foreground",e),...t}));d.displayName="CardDescription";let n=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-action",className:(0,a.cn)("col-start-2 row-span-2 row-start-1 self-start justify-self-end",e),...t}));n.displayName="CardAction";let l=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-content",className:(0,a.cn)("px-(--card-spacing)",e),...t}));l.displayName="CardContent";let c=t.forwardRef(({className:e,...t},s)=>(0,r.jsx)("div",{ref:s,"data-slot":"card-footer",className:(0,a.cn)("flex items-center rounded-b-xl px-(--card-spacing) [.border-t]:pt-(--card-spacing)",e),...t}));c.displayName="CardFooter",e.s(["Card",0,s,"CardAction",0,n,"CardContent",0,l,"CardDescription",0,d,"CardFooter",0,c,"CardHeader",0,o,"CardTitle",0,i])}]);