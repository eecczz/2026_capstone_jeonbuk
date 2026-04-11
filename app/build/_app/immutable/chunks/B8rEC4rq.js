import"./CWj6FrbW.js";import"./CN51-NxK.js";import{b as Oe,g as Ke,u as he,v as N,w as ze,e as Je,j as n,i as p,k as u,c as s,r as o,t as x,x as i,l as z,a as y,s as f,p as Qe,f as k,m as E,y as J,n as b}from"./Yu58oOUd.js";import{i as Ve}from"./TjyxkKSw.js";import{a as w,r as Q}from"./CHA-WQVD.js";import{b as V}from"./m85R-X74.js";import{b as ge}from"./DdgtKUyS.js";import{p as Xe}from"./BDD76Zze.js";import{i as Ze}from"./BZRIgjcJ.js";import{p as h}from"./CD805wuI.js";import{a as xe,s as et}from"./B0oqZymD.js";import{t as ye}from"./BYfhhmNr.js";import{g as tt}from"./BpsDBfjJ.js";import{u as rt}from"./C2F3WnYp.js";import{u as at}from"./BzsyB0T1.js";import{C as st}from"./mF_2HCA4.js";import{C as ot}from"./CFf702ku.js";import{C as it}from"./Bg4JOzkt.js";import{T as S}from"./DxsmdEH1.js";import{L as lt}from"./C8-71_gs.js";import{A as nt}from"./BiSDEcyS.js";var dt=k('<button class="w-full text-left text-sm py-1.5 px-1 rounded-lg dark:text-gray-300 dark:hover:text-white hover:bg-black/5 dark:hover:bg-gray-850" type="button"><!></button>'),ut=k('<input class="w-full text-2xl bg-transparent outline-hidden" type="text" required/>'),ct=k('<div class="text-sm text-gray-500 shrink-0"> </div>'),mt=k('<input class="w-full text-sm disabled:text-gray-500 bg-transparent outline-hidden" type="text" required/>'),ft=k('<input class="w-full text-sm bg-transparent outline-hidden" type="text" required/>'),vt=k('<div class="text-sm text-gray-500"><div class=" bg-yellow-500/20 text-yellow-700 dark:text-yellow-200 rounded-lg px-4 py-3"><div> </div> <ul class=" mt-1 list-disc pl-4 text-xs"><li> </li> <li> </li></ul></div> <div class="my-3"> </div></div>'),_t=k('<!> <div class=" flex flex-col justify-between w-full overflow-y-auto h-full"><div class="mx-auto w-full md:px-0 h-full"><form class=" flex flex-col max-h-[100dvh] h-full"><div class="flex flex-col flex-1 overflow-auto h-0 rounded-lg"><div class="w-full mb-2 flex flex-col gap-0.5"><div class="flex w-full items-center"><div class=" shrink-0 mr-2"><!></div> <div class="flex-1"><!></div> <div class="self-center shrink-0"><button class="bg-gray-50 hover:bg-gray-100 text-black dark:bg-gray-850 dark:hover:bg-gray-800 dark:text-white transition px-2 py-1 rounded-full flex gap-1 items-center" type="button"><!> <div class="text-sm font-medium shrink-0"> </div></button></div></div> <div class=" flex gap-2 px-1 items-center"><!> <!></div></div> <div class="mb-2 flex-1 overflow-auto h-0 rounded-lg"><!></div> <div class="pb-3 flex justify-between"><div class="flex-1 pr-3"><div class="text-xs text-gray-500 line-clamp-2"><span class=" font-semibold dark:text-gray-200"> </span> <br/>— <span class=" font-medium dark:text-gray-400"> </span></div></div> <button class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full" type="submit"> </button></div></div></form></div></div> <!>',1);function jt(be,v){Oe(v,!1);const _=()=>xe(rt,"$user",X),e=()=>xe(ke,"$i18n",X),[X,we]=et(),ke=Ke("i18n");let q=E(null),M=E(!1),j=E(!1),$=h(v,"edit",8,!1),Z=h(v,"clone",8,!1),$e=h(v,"onSave",8,()=>{}),T=h(v,"id",12,""),C=h(v,"name",12,""),P=h(v,"meta",28,()=>({description:""})),g=h(v,"content",12,""),A=h(v,"accessGrants",28,()=>[]),I=E("");const Te=()=>{p(I,g())};let D=E(),Ce=`import os
import requests
from datetime import datetime
from pydantic import BaseModel, Field

class Tools:
    def __init__(self):
        pass

    # Add your custom tools using pure Python code here, make sure to add type hints and descriptions
	
    def get_user_name_and_email_and_id(self, __user__: dict = {}) -> str:
        """
        Get the user name, Email and ID from the user object.
        """

        # Do not include a descrption for __user__ as it should not be shown in the tool's specification
        # The session user object will be passed as a parameter when the function is called

        print(__user__)
        result = ""

        if "name" in __user__:
            result += f"User: {__user__['name']}"
        if "id" in __user__:
            result += f" (ID: {__user__['id']})"
        if "email" in __user__:
            result += f" (Email: {__user__['email']})"

        if result == "":
            result = "User: Unknown"

        return result

    def get_current_time(self) -> str:
        """
        Get the current time in a more human-readable format.
        """

        now = datetime.now()
        current_time = now.strftime("%I:%M:%S %p")  # Using 12-hour format with AM/PM
        current_date = now.strftime(
            "%A, %B %d, %Y"
        )  # Full weekday, month name, day, and year

        return f"Current Date and Time = {current_date}, {current_time}"

    def calculator(
        self,
        equation: str = Field(
            ..., description="The mathematical equation to calculate."
        ),
    ) -> str:
        """
        Calculate the result of an equation.
        """

        # Avoid using eval in production code
        # https://nedbatchelder.com/blog/201206/eval_really_is_dangerous.html
        try:
            result = eval(equation)
            return f"{equation} = {result}"
        except Exception as e:
            print(e)
            return "Invalid equation"

    def get_current_weather(
        self,
        city: str = Field(
            "New York, NY", description="Get the current weather for a given city."
        ),
    ) -> str:
        """
        Get the current weather for a given city.
        """

        api_key = os.getenv("OPENWEATHER_API_KEY")
        if not api_key:
            return (
                "API key is not set in the environment variable 'OPENWEATHER_API_KEY'."
            )

        base_url = "http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": city,
            "appid": api_key,
            "units": "metric",  # Optional: Use 'imperial' for Fahrenheit
        }

        try:
            response = requests.get(base_url, params=params)
            response.raise_for_status()  # Raise HTTPError for bad responses (4xx and 5xx)
            data = response.json()

            if data.get("cod") != 200:
                return f"Error fetching weather data: {data.get('message')}"

            weather_description = data["weather"][0]["description"]
            temperature = data["main"]["temp"]
            humidity = data["main"]["humidity"]
            wind_speed = data["wind"]["speed"]

            return f"Weather in {city}: {temperature}°C"
        except requests.RequestException as e:
            return f"Error fetching weather data: {str(e)}"
`;const Ee=async()=>{$e()({id:T(),name:C(),meta:P(),content:g(),access_grants:A()})},ee=async()=>{if(n(D)){g(n(I)),await J();const a=await n(D).formatPythonCodeHandler();await J(),g(n(I)),await J(),a&&Ee()}};he(()=>N(g()),()=>{g()&&Te()}),he(()=>(N(C()),N($()),N(Z())),()=>{C()&&!$()&&!Z()&&T(C().replace(/\s+/g,"_").toLowerCase())}),ze(),Ze();var te=_t(),re=Je(te);{let a=b(()=>(_(),i(()=>{var t,r,l,m;return((l=(r=(t=_())==null?void 0:t.permissions)==null?void 0:r.sharing)==null?void 0:l.tools)||((m=_())==null?void 0:m.role)==="admin"}))),d=b(()=>(_(),i(()=>{var t,r,l,m;return((l=(r=(t=_())==null?void 0:t.permissions)==null?void 0:r.sharing)==null?void 0:l.public_tools)||((m=_())==null?void 0:m.role)==="admin"}))),c=b(()=>(_(),i(()=>{var t,r,l,m;return(((l=(r=(t=_())==null?void 0:t.permissions)==null?void 0:r.access_grants)==null?void 0:l.allow_users)??!0)||((m=_())==null?void 0:m.role)==="admin"})));nt(re,{accessRoles:["read","write"],get share(){return n(a)},get sharePublic(){return n(d)},get shareUsers(){return n(c)},onChange:async()=>{if($()&&T())try{await at(localStorage.token,T(),A()),ye.success(e().t("Saved"))}catch(t){ye.error(`${t}`)}},get show(){return n(j)},set show(t){p(j,t)},get accessGrants(){return A()},set accessGrants(t){A(t)},$$legacy:!0})}var H=u(re,2),ae=s(H),G=s(ae),se=s(G),U=s(se),W=s(U),F=s(W),qe=s(F);{let a=b(()=>(e(),i(()=>e().t("Back"))));S(qe,{get content(){return n(a)},children:(d,c)=>{var t=dt(),r=s(t);it(r,{strokeWidth:"2.5"}),o(t),x(l=>w(t,"aria-label",l),[()=>(e(),i(()=>e().t("Back")))]),z("click",t,()=>{tt("/workspace/tools")}),y(d,t)},$$slots:{default:!0}})}o(F);var R=u(F,2),Pe=s(R);{let a=b(()=>(e(),i(()=>e().t("e.g. My Tools"))));S(Pe,{get content(){return n(a)},placement:"top-start",children:(d,c)=>{var t=ut();Q(t),x((r,l)=>{w(t,"placeholder",r),w(t,"aria-label",l)},[()=>(e(),i(()=>e().t("Tool Name"))),()=>(e(),i(()=>e().t("Tool Name")))]),V(t,C),y(d,t)},$$slots:{default:!0}})}o(R);var oe=u(R,2),Y=s(oe),ie=s(Y);lt(ie,{strokeWidth:"2.5",className:"size-3.5"});var le=u(ie,2),Ae=s(le,!0);o(le),o(Y),o(oe),o(W);var ne=u(W,2),de=s(ne);{var Ie=a=>{var d=ct(),c=s(d,!0);o(d),x(()=>f(c,T())),y(a,d)},De=a=>{{let d=b(()=>(e(),i(()=>e().t("e.g. my_tools"))));S(a,{className:"w-full",get content(){return n(d)},placement:"top-start",children:(c,t)=>{var r=mt();Q(r),x((l,m)=>{w(r,"placeholder",l),w(r,"aria-label",m),r.disabled=$()},[()=>(e(),i(()=>e().t("Tool ID"))),()=>(e(),i(()=>e().t("Tool ID")))]),V(r,T),y(c,r)},$$slots:{default:!0}})}};Ve(de,a=>{$()?a(Ie):a(De,!1)})}var Ge=u(de,2);{let a=b(()=>(e(),i(()=>e().t("e.g. Tools for performing various operations"))));S(Ge,{className:"w-full self-center items-center flex",get content(){return n(a)},placement:"top-start",children:(d,c)=>{var t=ft();Q(t),x((r,l)=>{w(t,"placeholder",r),w(t,"aria-label",l)},[()=>(e(),i(()=>e().t("Tool Description"))),()=>(e(),i(()=>e().t("Tool Description")))]),V(t,()=>P().description,r=>P(P().description=r,!0)),y(d,t)},$$slots:{default:!0}})}o(ne),o(U);var B=u(U,2),Ne=s(B);ge(st(Ne,{get value(){return g()},lang:"python",boilerplate:Ce,onChange:a=>{p(I,a)},onSave:async()=>{n(q)&&n(q).requestSubmit()},$$legacy:!0}),a=>p(D,a),()=>n(D)),o(B);var ue=u(B,2),L=s(ue),ce=s(L),O=s(ce),Se=s(O,!0);o(O);var me=u(O),fe=u(me,3),Me=s(fe,!0);o(fe),o(ce),o(L);var ve=u(L,2),je=s(ve,!0);o(ve),o(ue),o(se),o(G),ge(G,a=>p(q,a),()=>n(q)),o(ae),o(H);var He=u(H,2);ot(He,{get show(){return n(M)},set show(a){p(M,a)},$$events:{confirm:()=>{ee()}},children:(a,d)=>{var c=vt(),t=s(c),r=s(t),l=s(r,!0);o(r);var m=u(r,2),K=s(m),Ue=s(K,!0);o(K);var _e=u(K,2),We=s(_e,!0);o(_e),o(m),o(t);var pe=u(t,2),Fe=s(pe,!0);o(pe),o(c),x((Re,Ye,Be,Le)=>{f(l,Re),f(Ue,Ye),f(We,Be),f(Fe,Le)},[()=>(e(),i(()=>e().t("Please carefully review the following warnings:"))),()=>(e(),i(()=>e().t("Tools have a function calling system that allows arbitrary code execution."))),()=>(e(),i(()=>e().t("Do not install tools from sources you do not fully trust."))),()=>(e(),i(()=>e().t("I acknowledge that I have read and I understand the implications of my action. I am aware of the risks associated with executing arbitrary code and I have verified the trustworthiness of the source.")))]),y(a,c)},$$slots:{default:!0},$$legacy:!0}),x((a,d,c,t,r)=>{f(Ae,a),f(Se,d),f(me,` ${c??""} `),f(Me,t),f(je,r)},[()=>(e(),i(()=>e().t("Access"))),()=>(e(),i(()=>e().t("Warning:"))),()=>(e(),i(()=>e().t("Tools are a function calling system with arbitrary code execution"))),()=>(e(),i(()=>e().t("don't install random tools from sources you don't trust."))),()=>(e(),i(()=>e().t("Save")))]),z("click",Y,()=>{p(j,!0)}),z("submit",G,Xe(()=>{$()?ee():p(M,!0)})),y(be,te),Qe(),we()}export{jt as T};
