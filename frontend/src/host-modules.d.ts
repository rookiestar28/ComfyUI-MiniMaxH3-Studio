declare module "*scripts/app.js" {
  export const app: import("./host/hostModuleTypes").HostApp;
}

declare module "*scripts/api.js" {
  export const api: import("./host/hostModuleTypes").HostApi;
}

declare module "*.css?inline" {
  const content: string;
  export default content;
}
