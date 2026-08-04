# 综述模板写作方式与风格总结

本文档基于 `/home/ps/review-writer/template` 下三篇综述模板，并结合 MinerU 转换后的 Markdown、图片和 `content_list.json` 总结其写作组织方式。

参考模板：

```text
allenation-of-terminal-alkynes-with-aldehydes-and-ketones.pdf
Angew Chem Int Ed - 2016 - Jia - New Approaches to the Synthesis of Metal Carbenes.pdf
Conquering three-carbon axial chirality of allenes.pdf
```

MinerU 输出位置：

```text
/home/ps/review-writer/mineru-outputs/markdown/
/home/ps/review-writer/mineru-outputs/extracted/
```

## 1. 三篇模板的基本类型

### 1.1 Account 型：围绕一个研究体系的发展线展开

代表：

```text
Allenation of Terminal Alkynes with Aldehydes and Ketones
```

这篇更像研究团队对自身系列工作的系统回顾。文章不是简单罗列领域全部文献，而是围绕 ATA/EATA 这一条主线展开：

```text
早期反应局限
改进反应条件
扩展底物类型
发展不对称版本
应用到天然产物合成
总结未解决问题
```

它的核心特点是“问题推进式”组织。每一节都回答一个具体发展问题，例如从 paraformaldehyde 到普通 aldehyde，再到 ketone，再到 enantioselective version。

适合用于：

```text
综述主题比较集中
文献主要围绕一个反应体系或一个课题组长期发展
需要写出技术演进和方法升级
```

### 1.2 大型 Review 型：按来源或类别全面铺开

代表：

```text
New Approaches to the Synthesis of Metal Carbenes
```

这篇是标准大型综述。文章主题很宽，因此采用“分类目录式”组织：

```text
Hydrazones
Amines
Phenyliodonium and Sulfonium Ylides
Triazoles
Cycloheptatrienes
Cyclopropenes
Propargylic Esters and Alcohols
Enynes
Alkynes
Allenes
```

每一大类下面再按反应类型拆成小节：

```text
cyclopropanation
X-H insertion
reaction with nucleophiles
migratory insertion
1,2-migration
cycloaddition
```

它的核心特点是“分类覆盖式”组织。文章用清晰目录帮助读者快速定位不同前体或反应模式，适合覆盖大量文献。

适合用于：

```text
综述主题很大
文献数量多
需要形成一个领域地图
可按底物、前体、催化体系或反应类型分类
```

### 1.3 专题方法型：按合成来源或策略分类

代表：

```text
Conquering three-carbon axial chirality of allenes
```

这篇介于 Account 和大型 Review 之间。主题集中在 axially chiral allenes，但组织方式不是按时间，而是按“从哪里合成”分类：

```text
from propargylic alcohols or derivatives
from terminal alkynes, aldehydes, and amines
from conjugated enynes
from ketenes
from propargyl/allenyl anion intermediates
from racemic allenes
from enol triflates
```

它的核心特点是“入口分类式”组织。每一类方法都先说明基本反应逻辑，再比较代表性方法的优势、局限和机制差异。

适合用于：

```text
主题中等规模
方法来源清楚
希望突出不同合成策略之间的比较
```

## 2. 开头部分的组织方式

三篇模板的开头都遵循类似逻辑：

```text
1. 先说明研究对象为什么重要
2. 再说明已有方法或传统认识的不足
3. 然后引出本文综述范围
4. 最后提前告诉读者文章如何组织
```

### 2.1 重要性铺垫

开头通常不会直接进入某篇论文，而是先建立主题价值：

```text
该结构或中间体在天然产物、药物、合成转化中重要
该反应模式能解决传统方法难以解决的问题
该领域近年快速发展，值得系统总结
```

例如 allene 相关模板会先强调：

```text
allene 存在于天然产物和药物中
allene 具有轴手性和独特反应性
allene 可用于手性转移和复杂分子合成
```

### 2.2 痛点设置

开头通常会给出领域瓶颈：

```text
传统方法底物范围有限
反应条件苛刻
手性控制困难
试剂不安全或不稳定
催化不对称版本发展不足
```

这一步很重要，因为后文每个章节都应该回应这些痛点。

### 2.3 范围声明

模板中常见写法是直接说明“本文总结什么，不总结什么”。

可以模仿为：

```text
This review summarizes selected advances in ...
This account describes our efforts toward ...
We present a critical rather than exhaustive account of ...
```

对中文写作流程而言，应转化为：

```text
本文重点总结……
本文不追求穷尽所有文献，而重点讨论……
下文将按照……进行组织。
```

## 3. 主体内容的组织方式

### 3.1 不以单篇论文为默认单位

三篇模板都不是简单的“某某报道了什么，某某又报道了什么”的堆叠。更常见的段落单位是：

```text
一个方法类别
一个反应问题
一个机制解释
一个底物范围变化
一个局限或改进方向
```

一篇论文通常作为某个观点或方法节点的证据出现，而不是段落本身的唯一目的。

### 3.2 每节通常有固定内部结构

多数小节遵循这个模式：

```text
1. 说明该类方法的基本逻辑或历史起点
2. 给出代表性初始工作
3. 介绍后续改进或扩展
4. 讨论底物范围、选择性、条件和机制
5. 点出仍然存在的问题
```

例如在 chirality 模板中，一个小节常见推进方式是：

```text
早期 chirality transfer 方法
影响 chirality transfer 的因素
可能机制路径
避免 racemization 的新策略
代表性催化不对称突破
仍未解决的底物或选择性问题
```

### 3.3 章节之间要有推进关系

好的综述不是并列堆放。模板中的章节关系通常是：

```text
从简单到底物更复杂
从 racemic 到 asymmetric
从方法建立到合成应用
从传统试剂到安全替代物
从已成熟体系到仍待发展的体系
```

这种推进关系可以用于后续 agent 写作：

```text
先讲基础反应和历史背景
再讲关键改进
再讲高级版本或不对称版本
最后讲应用和未来问题
```

## 4. 图和 Scheme 的使用方式

三篇模板都高度依赖 Scheme。

MinerU 统计显示：

```text
Allenation Account: image/chart/table block 很多，核心是 Scheme 和应用图
Metal Carbenes Review: 100 个以上 scheme，几乎每个小节都有图
Axial Chirality Review: 大量 scheme 用于对比不同合成入口和机制
```

### 4.1 图不是装饰，而是章节骨架

模板里的 Scheme 通常承担这些功能：

```text
定义反应类型
展示代表性条件
比较底物范围
解释机制路径
显示合成应用
总结分类关系
```

在有机综述中，很多段落实际上是围绕一个 Scheme 展开的。

### 4.2 典型写法是“文字引出 + Scheme 支撑 + 文字解释”

常见结构：

```text
先用一两句话说明问题或方法
插入 Scheme
再解释 Scheme 中最重要的产率、选择性、底物范围或机制含义
最后指出局限或后续改进
```

不建议写成：

```text
插入图之后不解释图
只描述图里所有分子细节
把图当成论文截图堆放
```

### 4.3 机制图和总览图尤其重要

模板中高价值图通常包括：

```text
历史起点反应
核心机制图
策略对比图
底物范围代表图
天然产物应用图
总览分类图
```

后续综述写作时，优先选择能支撑章节论点的图，而不是每篇论文都配一张图。

## 5. 段落写作风格

### 5.1 语气客观克制

模板中的评价通常比较克制，常用表达是：

```text
proved to be
has been demonstrated
was realized
is still challenging
remains underdeveloped
further exploration is desirable
```

中文写作时可以对应为：

```text
已被证明……
实现了……
仍然具有挑战……
仍处于发展不足阶段……
有待进一步拓展……
```

避免过度宣传式表达，例如：

```text
革命性突破
极其完美
完全解决
开创了全新时代
```

除非文献证据确实支持。

### 5.2 每段要有“判断”

模板段落不是只给事实，还会给出判断：

```text
为什么这个方法重要
它相比前人解决了什么
它适合什么底物
它的限制在哪里
它为什么需要后续改进
```

后续 agent 写作时，每段最好包含：

```text
中心观点
代表文献
关键证据
比较或限制
过渡到下一点
```

### 5.3 局限要自然嵌入

三篇模板都会在介绍成功结果后补充不足，例如：

```text
scope remains limited
reactivity is lower
enantioselectivity is moderate
substrate class has not been realized
the reaction requires harsh conditions
```

这让综述显得有批判性，而不是单纯赞美已有工作。

## 6. 结论部分的组织方式

三篇模板的结论都不是简单重复摘要，而是按下面逻辑收束：

```text
1. 概括已经取得的主要进展
2. 指出当前领域仍未解决的问题
3. 提出未来最可能的研究方向
4. 用简短判断说明该领域仍值得继续发展
```

### 6.1 Account 型结论

Account 型结论通常先总结本团队或该体系完成了什么：

```text
发展了 racemic version
发展了 asymmetric version
实现了天然产物应用
```

然后列出具体挑战：

```text
更温和条件
更通用的 chiral ligand strategy
ketone 的不对称版本
```

### 6.2 大型 Review 型结论

大型 Review 的结论通常先做领域地图式总结：

```text
哪些 surrogate 已成熟
哪些底物类型发展迅速
哪些金属和配体调控关键
哪些方向仍然探索不足
```

然后提出方向：

```text
更廉价金属
新配体
机制理解
复杂分子合成应用
```

### 6.3 专题方法型结论

专题方法型结论常按未来问题分条推进：

```text
催化不对称方法仍需发展
有机催化仍不足
特定消除策略有潜力
全新方法仍值得期待
```

## 7. 可直接用于后续 agent 的写作规范

后续综述 agent 写作时，应优先采用以下规则。

### 7.1 大纲设计规则

根据主题选择一种主组织逻辑：

```text
课题发展线：适合单一反应体系或一个技术路线
底物/前体分类：适合文献多、主题宽的领域综述
方法来源分类：适合中等主题、多个合成入口并存的综述
反应类型分类：适合按 cyclization、coupling、migration 等反应模式比较
应用导向分类：适合最后一章或偏应用型综述
```

不要混乱叠加多个分类标准。大纲一级标题最好只采用一个主分类标准。

### 7.2 每个章节任务书应包含

```text
本节解决什么问题
为什么这一类方法值得单独成节
本节涉及哪些核心文献
本节需要哪些 Scheme 或图
本节最后要指出什么局限
如何过渡到下一节
```

### 7.3 每个段落应包含

```text
中心观点
代表性例子
关键结果或机制
比较对象
局限或意义
```

### 7.4 图表选择规则

优先选择：

```text
能解释机制的图
能代表一类方法的 Scheme
能比较多个条件或底物的表
能展示应用价值的合成路线
能作为章节总览的分类图
```

避免：

```text
每篇文献机械配一张图
选择只展示少量普通底物的图
选择和正文论点关系弱的图
没有解释图就直接放图
```

### 7.5 结论写法规则

结论应该按这个顺序写：

```text
已取得的核心进展
目前仍存在的关键限制
未来最值得发展的方向
该领域继续研究的价值
```

不要只重复每章标题。

## 8. 对当前综述 agent 流程的启发

这三篇模板说明，有机综述质量主要取决于四点：

```text
大纲是否有单一清晰的分类逻辑
每节是否围绕问题推进，而不是堆论文
Scheme 是否服务于论点，而不是装饰
结论是否提出真实限制和未来方向
```

因此后续 agent 写作时，应把模板风格转化为以下执行原则：

```text
先确定文章组织逻辑，再写章节
每节写作前明确本节的核心问题
每个 Scheme 必须绑定一个段落论点
每段尽量做比较、解释或判断
每章末尾自然指出不足，为下一章或结论铺垫
```

这比单纯提高引用数量更重要。
