import { Injectable } from '@angular/core';

export interface Product {
  group: string
  name: string
  qty: number
  qlt: string
  checked: boolean
  id: number
}

@Injectable({
  providedIn: 'root'
})
export class DataService {
  public products: Product[] = [
    {
      group: "Молочные продукты",
      name: 'Молоко',
      qty: 1,
      qlt: 'л',
      checked: false,
      id: 1
    },
    {
      group: "Молочные продукты",
      name: 'сыр Масдам',
      qty: 40,
      qlt: 'г',
      checked: true,
      id: 2
    },
    {
      group: "Овощи",
      name: 'Картошка',
      qty: 2,
      qlt: 'кг',
      checked: false,
      id: 3
    },
    {
      group: "Овощи",
      name: 'Морковка',
      qty: 4,
      qlt: 'шт',
      checked: false,
      id: 4
    },
    {
      group: "Мясо",
      name: 'Говядина',
      qty: 1.5,
      qlt: 'кг',
      checked: false,
      id: 5
    },
    {
      group: "Мясо",
      name: 'Свинина',
      qty: 1,
      qlt: 'кг',
      checked: false,
      id: 6
    },
    {
      group: "Мясо",
      name: 'Куриные грудки',
      qty: 4,
      qlt: 'шт',
      checked: false,
      id: 7
    },
    {
      group: "Зелень",
      name: 'Укроп',
      qty: 1,
      qlt: 'Пучок',
      checked: false,
      id: 8
    }
  ];

  constructor() { }

  public getProducts(): Product[] {
    return this.products;
  }

  public getProductById(id: number): Product {
    return this.products[id];
  }
}
