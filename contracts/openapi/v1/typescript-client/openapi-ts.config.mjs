/** @type {import('@hey-api/openapi-ts').UserConfig} */
export default {
  input: '../openapi.yaml',
  output: {
    clean: true,
    path: '../../../generated/typescript/control-api',
  },
};
