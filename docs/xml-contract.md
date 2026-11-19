# XML 入口契约与存储约定

本文档只回答一件事：**XML 读入 `benedict` 之后，按什么结构存，才能让下游稳定地区分"一项"与"多项"、区分空值与缺失、读写属性和子元素。**

暂不修改 `benedict` 的公开行为。当前 `from_xml` / `to_xml` 是 `xmltodict` 的薄封装（`benedict/serializers/xml.py`），本文档记录它的实际语义、失真点，以及一套可以立即在调用侧落地的稳定契约。

## 1. 现状：入口就是 xmltodict 默认值

```python
# benedict/serializers/xml.py
def decode(self, s, **kwargs):
    kwargs.setdefault("dict_constructor", dict)
    return xmltodict.parse(s, **kwargs)

def encode(self, d, **kwargs):
    return xmltodict.unparse(d, **kwargs)
```

所有语义都由 `xmltodict` 决定，`benedict` 只把 `kwargs` 原样透传：

- 文本一律是 `str`，不推断数字、布尔、`None`。
- 属性键加前缀 `@`，文本键加前缀 `#text`。
- 同名子元素 **1 个是 `dict`、2 个及以上是 `list`**。
- 空元素（自闭合、空对、纯空白）都变成 `None`。
- 顶层只允许一个根元素，否则报 `ValueError`。

这不是 `benedict` 的 bug，而是 `xmltodict` 的既定映射。但它意味着 **Python 侧的类型不稳定**，下游无法用 `isinstance(x, list)` 判断"业务上是集合"。

## 2. 两个样例

样例 A（一项、自闭合空元素、有属性）：

```xml
<order id="A-1">
  <item sku="X">book</item>
  <note/>
</order>
```

样例 B（重复子元素、显式空对、空白文本、有属性）：

```xml
<order id="B-2">
  <item sku="X">book</item>
  <item sku="Y"></item>
  <note>   </note>
</order>
```

### 2.1 实际解码结果

```python
benedict.from_xml(A)
# {'order': {'@id': 'A-1',
#            'item': {'@sku': 'X', '#text': 'book'},   # dict
#            'note': None}}

benedict.from_xml(B)
# {'order': {'@id': 'B-2',
#            'item': [{'@sku': 'X', '#text': 'book'},  # list
#                     {'@sku': 'Y'}],
#            'note': None}}
```

注意 B 的第二个 `item` 是 `{'@sku': 'Y'}`，**没有 `#text` 键**——因为原文 `></item>` 里没有文本，`xmltodict` 不生成该键。

### 2.2 在哪里失去区分

| 失真点 | A | B | 结果 |
|---|---|---|---|
| **基数（一项 vs 多项）** | `item` 是 `dict` | `item` 是 `list` | 同一业务字段类型漂移，`item["@sku"]` 在 B 上直接报错/返回 `None` |
| **自闭合 vs 空对** | `<note/>` | `<note></note>` | 都是 `None`，语法差异丢失 |
| **空 vs 纯空白** | `<note/>` | `<note>   </note>` | 都是 `None`（默认 `strip_whitespace=True`），空白文本丢失 |
| **有文本 vs 无文本** | `item` 含 `#text` | 第二个 `item` 无 `#text` | "空字符串"与"无文本节点"不可分 |
| **属性 vs 子元素同名** | `@id` 与 `id` 分处不同键 | 同左 | 键前缀 `@` 救了这一例，但 `@` 在 XML 名中非法，属于约定而非结构保证 |
| **元素顺序** | 无重复，顺序隐含 | `item,item,note` | 解码为 `dict` 后只保留"首次出现键"的顺序，重复项被聚合，交错顺序丢失 |

### 2.3 回写会进一步变形

```python
benedict.from_xml(A).to_xml()
# <order id="A-1"><item sku="X">book</item><note></note></order>
# 自闭合 <note/> 变成 <note></note>

benedict.from_xml(B).to_xml()
# <order id="B-2"><item sku="X">book</item><item sku="Y"></item><note></note></order>
# <note>   </note> 的空白文本丢失
```

`to_xml(short_empty_elements=True)` 可以恢复 `<note/>` 写法，但**无法恢复"空对"与"自闭合"的选择**，因为解码后两者都已是 `None`。同理，`<item>   </item>` 的空白在默认解码后已不可恢复；只有解码时显式传 `strip_whitespace=False` 才会保留为 `'   '`。

## 3. 稳定入口契约（调用侧，不改 benedict）

推荐所有 XML 入口统一走下面这层薄包装。它不改变存储格式，只是**把"哪些键是集合"从数据里提出来，作为调用参数固化**。

### 3.1 读：显式声明集合字段

```python
from benedict import benedict

def load_order(xml: str) -> benedict:
    return benedict.from_xml(
        xml,
        # 集合字段必须在 schema 层声明，不依赖 XML 里恰好出现了几次
        force_list=collection_fields,
        # 需要区分"空文本"与"无文本"时打开；默认 True 会吞掉空白
        strip_whitespace=False,
        # 需要保留注释/命名空间时按需打开
        # process_comments=True,
        # process_namespaces=True,
    )
```

`collection_fields` 就是下方 3.1 末尾给出的 callable，集合字段清单来自业务 schema，而不是本次 XML 里出现了几次。

`force_list` 的语义（`xmltodict` 1.0.4 实测）：

- `force_list=True`：所有元素都变成单元素列表；
- `force_list=("item",)`：**按元素名全局生效**，嵌套的同名元素也会被强制成列表；
- `force_list=callable(path, key, value) -> bool`：可按路径精确限定，`path` 是 `(tag, attrs)` 元组序列。

按路径限定的写法（推荐，避免误伤嵌套同名元素）：

```python
def collection_fields(path, key, value):
    tags = tuple(tag for tag, _attrs in path)
    return key == "item" and tags == ("order",)
```

> 注意 `force_list=True` 会把**所有**元素（包括根）包成列表，`benedict.from_xml(...)["order"]` 会变成 `list`，根层级不再可按键访问。因此**不要用 `True`**，用元组或 callable。

`force_list` 只稳定"元素存在时的基数"。元素**完全缺失**时不会凭空生成键：

```python
benedict.from_xml("<order><note/></order>", force_list=("item",))
# {'order': {'note': None}}   # 没有 'item' 键，而不是 {'item': []}
benedict.from_xml("<order/>", force_list=("item",))
# {'order': None}             # 父元素本身无内容时也会变成 None
```

如果契约要求"集合字段永远存在且为空时是 `[]`"，需要在入口包装里补默认值，见 3.3。

### 3.2 存储结构：单一形态

用 `force_list` 后，A 与 B 的解码结果结构一致：

```python
load_order(A)
# {'order': {'@id': 'A-1',
#            'item': [{'@sku': 'X', '#text': 'book'}],
#            'note': None}}

load_order(B)
# {'order': {'@id': 'B-2',
#            'item': [{'@sku': 'X', '#text': 'book'}, {'@sku': 'Y'}],
#            'note': None}}
```

约定：

- **集合字段永远是 `list`**，即使只有 1 项；空集合是 `[]`，不是 `None`。
- **标量字段**（`@id`、`#text`）永远是 `str`，不转类型。
- **空元素**保留为 `None`，与"字段缺失"（键不存在）区分开。若要区分"空字符串"与"无文本"，解码时开 `strip_whitespace=False`，并把 `#text` 缺失视为"无文本节点"。
- **属性**统一是 `@name` 键；`to_xml` 依赖这个前缀，不要在下游剥掉前缀后又塞回去。

`force_list` 只保证"出现时是列表"，元素缺失时不会补键。要让"集合字段恒存在"成立，入口统一做一次归一化：

```python
COLLECTIONS = {("order", "item")}

def normalize(data: dict) -> dict:
    for parent, child in COLLECTIONS:
        node = data.get(parent)
        if isinstance(node, dict):
            node.setdefault(child, [])
            if not isinstance(node[child], list):
                node[child] = [node[child]]
    return data


def load_order(xml: str) -> benedict:
    return benedict(
        normalize(
            benedict.from_xml(
                xml,
                force_list=collection_fields,
                strip_whitespace=False,
            ).dict()
        )
    )
```

这样 A、B、以及完全没有 `item` 的订单，三者在 `order.item` 上都是 `list`。`<order/>` 这种父元素本身为空的输入仍会得到 `{'order': None}`，需要在 `normalize` 里一并处理成 `{'order': {'item': []}}`；是否这样做取决于业务是否允许空订单。

### 3.3 写：先归一化，再编码

```python
def dump_order(d: benedict) -> str:
    data = d.dict()
    order = data["order"]
    # 单值入口也要转成列表，保证写出的是重复元素
    if not isinstance(order.get("item"), list):
        order["item"] = [order["item"]]
    return benedict(data).to_xml(
        pretty=True,
        short_empty_elements=True,  # 空元素写成 <note/>
    )
```

## 4. 需要额外元信息的数据

以下信息**无法**从 `benedict` 解码后的普通 `dict`/`list` 结构里恢复，必须额外保存。按重要性排序：

| 需要的元信息 | 为什么结构里没有 | 建议存法 |
|---|---|---|
| **元素顺序（尤其交错重复）** | `dict` 按键聚合，`<a/><b/><a/>` 解码为 `a: [...], b: ...` | 在 schema 层单独存 `order: ["a", "b", "a"]`，或直接保留原始 XML |
| **元素名与属性名的原始前缀/命名空间** | 默认 `process_namespaces=False`，`xmlns:o` 只是普通属性；开启后键会变成 `urn:o:item` | 存 `{prefix: uri}` 映射 + 原始限定名，或保持 `process_namespaces=False` 并约定不改写 |
| **空元素的原始写法**（`<x/>` vs `<x></x>`） | 两者都解码为 `None` | 若必须逐字节还原，只能保留原始 XML；否则约定统一写 `<x/>` |
| **纯空白文本**（`<x>   </x>`） | 默认 `strip_whitespace=True` 吞成 `None` | 解码时 `strip_whitespace=False`，并额外标记"该 `#text` 是空白" |
| **"空字符串文本" vs "无文本节点"** | 空对不生成 `#text` 键，`<x></x>` 与 `<x> </x>` 都塌缩 | 存 `#text` 是否存在 + 是否 `strip_whitespace` 的标志位 |
| **注释、处理指令、CDATA 边界** | 默认丢弃或合并成普通文本 | `process_comments=True` 保留注释；CDATA 需 `force_cdata=True` 才会以 `#text` 形式保留边界信息；PI 无法保留 |
| **文本与子元素的混排顺序** | `#text` 只有一个键，混合内容被压平 | 必须保留原始 XML，或改用能表达 mixed content 的模型 |
| **数字/布尔的原始字面量** | 一律 `str`，`007` 与 `7` 需下游自行解释 | 若要做类型化，schema 里声明类型 + 保留原始字符串 |

上表中只有前三项属于"结构层面不可区分"；后几项可以通过解码参数部分保留，但都会让下游多出额外的键（`#text`、`#comment`），需要 schema 一并声明。

## 5. 契约边界

- 这套契约**不引入新的存储格式**，仍然是一个嵌套 `dict`，因此 `benedict` 的 keypath / keylist / 过滤等能力照常可用。
- `force_list` 是**调用参数，不是数据的一部分**。一旦数据被序列化成 JSON 或存库，这个信息就丢了；因此集合字段清单必须随 schema 一起持久化。
- 键名含 `.` 会被 `benedict` 的 keypath 校验拒绝（`ValueError: Key should not contain keypath separator`）。来自 XML 的元素名如果可能含 `.`，入口要么改 `keypath_separator`，要么用 `keypath_separator=None`。
- 顶层必须恰好一个根元素；`<a/><b/>` 这种多根片段不能直接 `from_xml`，需要先包一层根或走 `xmltodict.parse` 后自行处理。

## 6. 回归样例

`tests/dicts/io/test_io_dict_xml_contract.py` 把 A、B 两个样例的当前行为固定下来，包括：

- 默认解码的基数漂移（A 是 `dict`，B 是 `list`）；
- `force_list` 后两者结构一致；
- 自闭合、空对、空白文本都塌缩为 `None`；
- 属性 `@id` / `@sku` 与文本 `#text` 的往返；
- 回写时 `short_empty_elements` 的效果与不可恢复项。

运行：

```bash
python3 -m pytest tests/dicts/io/test_io_dict_xml_contract.py -q
```
