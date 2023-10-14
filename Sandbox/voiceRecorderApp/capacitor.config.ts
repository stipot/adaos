import { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'my.app',
  appName: 'my-app',
  webDir: 'www',
  bundledWebRuntime: false,
  server: {
    cleartext: true,
  },
};

export default config;
