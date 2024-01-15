import { Component, OnInit } from '@angular/core';
import { RefresherCustomEvent } from '@ionic/angular';
import { DataService, Product } from '../services/data.service';
import { SpeechRecognition } from '@capacitor-community/speech-recognition';

@Component({
  selector: 'app-home',
  templateUrl: 'home.page.html',
  styleUrls: ['home.page.scss'],
})
export class HomePage implements OnInit {
  recording: boolean = false;
  text: any;

  constructor(private data: DataService) {
    SpeechRecognition.requestPermissions();
  }
  product?: Product;
  refresh(ev: any) {
    setTimeout(() => {
      (ev as RefresherCustomEvent).detail.complete();
    }, 3000);
  }
  ngOnInit(): void {}

  async startRecognation() {
    const { available } = await SpeechRecognition.available();
    if (available) {
      this.recording = true;
      this.text = await SpeechRecognition.start({
        popup: false,
        partialResults: false,
        language: 'ru-RU',
      });

      if (this.text && this.text.matches.length > 0) {
        this.text = this.text.matches[0];
      }
    }
  }

  async stopRecognation() {
    this.recording = false;
    await SpeechRecognition.stop();
  }
}
