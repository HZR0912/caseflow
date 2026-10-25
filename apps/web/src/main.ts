import { createApp } from "vue";
import App from "./App.vue";
import TraceApp from "./TraceApp.vue";
import "./style.css";
createApp(location.pathname === "/traces" ? TraceApp : App).mount("#app");
