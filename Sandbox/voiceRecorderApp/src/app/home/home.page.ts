import { Component, OnInit } from '@angular/core';
import { RefresherCustomEvent } from '@ionic/angular';
import { ColdObservable } from 'rxjs/internal/testing/ColdObservable';

import { DataService, Product } from '../services/data.service';
import { Directory, Filesystem } from '@capacitor/filesystem';
import { RecordingData, VoiceRecorder } from 'capacitor-voice-recorder';

@Component({
  selector: 'app-home',
  templateUrl: 'home.page.html',
  styleUrls: ['home.page.scss'],
})
export class HomePage implements OnInit {
  recording: boolean = false;
  storedFileNames: any = [];

  constructor(private data: DataService) {}
  product?: Product;
  refresh(ev: any) {
    setTimeout(() => {
      (ev as RefresherCustomEvent).detail.complete();
    }, 3000);
  }

  /*   getProiductsGroups() {
    return this.data.getProducts().reduce(
      (entryMap, e) => entryMap.set(e.group, [...entryMap.get(e.group) || [], e]),
      new Map()
    )
  } */

  ngOnInit(): void {
    VoiceRecorder.requestAudioRecordingPermission();
    this.loadFiles();
  }

  async loadFiles() {
    Filesystem.readdir({
      path: '',
      directory: Directory.Data,
    }).then((result) => {
      this.storedFileNames = result.files;
    });
  }

  startRecording(): void {
    if (this.recording) {
      return;
    }
    this.recording = true;
    VoiceRecorder.startRecording();
  }

  stopRecording(): void {
    if (!this.recording) {
      return;
    }
    VoiceRecorder.stopRecording().then(async (result: RecordingData) => {
      if (result.value && result.value.recordDataBase64) {
        const recordData = result.value.recordDataBase64;
        console.log('Stop recording ');
        const fileName = new Date().getTime() + '.wav';
        await Filesystem.writeFile({
          path: fileName,
          directory: Directory.Data,
          data: recordData,
        });
        this.loadFiles();
      }
    });
    this.recording = false;
  }

  async deleteFile(fileName: string) {
    await Filesystem.deleteFile({
      path: fileName,
      directory: Directory.Data,
    });

    this.loadFiles();
  }

  async playFile(fileName: string) {
    const audioFile = await Filesystem.readFile({
      path: fileName,
      directory: Directory.Data,
    });
    console.log('playFile: ', audioFile);
    const base64Sound = audioFile.data;

    const audioRef = new Audio(`data:audio/mpeg;base64,${base64Sound}`);
    audioRef.oncanplaythrough = () => audioRef.play();
    audioRef.load();
  }

  getProiducts(): Product[] {
    return this.data.getProducts();
  }
}
