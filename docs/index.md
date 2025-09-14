# AdaOS

> «Мультиагентная многопользовательская ОС навыков и сценариев» — минимализм ядра, максимум гибкости на краях.

!!! tip "зачем читать"
    Быстро понять архитектуру, запустить демо, написать первый навык/сценарий и подключить LLM как разработчика.

## Что такое AdaOS

AdaOS — это платформа для управления **навыками** (skills) и **сценариями** (scenarios) с упором на DevOps, интерпретируемость и кросс-ОС.

## Архитектура одним взглядом

```dot
digraph G {
  rankdir=LR;
  node [shape=box, style=rounded];

  subgraph cluster_core {
    label="Core";
    Agent; Runtime; Scheduler;
    Agent -> Runtime -> Scheduler;
  }

  subgraph cluster_devops {
    label="DevOps";
    CLI; API; Tests; Docs;
    CLI -> Agent;
    API -> Agent;
    Tests -> Runtime;
    Docs -> CLI;
  }

  Runtime -> Skills [label="uses"];
  Runtime -> Scenarios [label="uses"];

  Skills [shape=component];
  Scenarios [shape=component];
}
````

## Установка

```bash
pip install -e ".[dev]"
```

## Первый запуск

```bash
adaos api serve
```
