using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace Valtrion.Unity.Json
{
    public enum JsonKind
    {
        Null,
        Bool,
        Number,
        String,
        Array,
        Object,
    }

    /// <summary>A parsed JSON value. Plan steps, metric maps and event data are free-form, so the Runtime keeps them as a tree
    /// and maps the typed contract classes to and from it by explicit code (no reflection, AOT safe).</summary>
    public sealed class JsonValue
    {
        static readonly JsonValue NullValue = new JsonValue(JsonKind.Null);
        static readonly JsonValue TrueValue = new JsonValue(JsonKind.Bool) { _bool = true };
        static readonly JsonValue FalseValue = new JsonValue(JsonKind.Bool);

        bool _bool;
        double _number;
        string _string;
        List<JsonValue> _items;
        List<KeyValuePair<string, JsonValue>> _members;

        JsonValue(JsonKind kind)
        {
            Kind = kind;
        }

        public JsonKind Kind { get; }

        public static JsonValue Null => NullValue;

        public static JsonValue From(bool value) => value ? TrueValue : FalseValue;

        public static JsonValue From(double value)
        {
            if (double.IsNaN(value) || double.IsInfinity(value))
                throw new ArgumentOutOfRangeException(nameof(value), "JSON has no NaN or infinity");
            return new JsonValue(JsonKind.Number) { _number = value };
        }

        public static JsonValue From(string value) => value == null ? NullValue : new JsonValue(JsonKind.String) { _string = value };

        public static JsonValue NewArray() => new JsonValue(JsonKind.Array) { _items = new List<JsonValue>() };

        public static JsonValue NewObject() => new JsonValue(JsonKind.Object) { _members = new List<KeyValuePair<string, JsonValue>>() };

        public bool AsBool => Kind == JsonKind.Bool ? _bool : throw Wrong(JsonKind.Bool);

        public double AsNumber => Kind == JsonKind.Number ? _number : throw Wrong(JsonKind.Number);

        public string AsString => Kind == JsonKind.String ? _string : throw Wrong(JsonKind.String);

        public IReadOnlyList<JsonValue> Items => Kind == JsonKind.Array ? _items : throw Wrong(JsonKind.Array);

        public IReadOnlyList<KeyValuePair<string, JsonValue>> Members => Kind == JsonKind.Object ? _members : throw Wrong(JsonKind.Object);

        public int Count => Kind == JsonKind.Array ? _items.Count : Kind == JsonKind.Object ? _members.Count : throw Wrong(JsonKind.Array);

        public JsonValue Add(JsonValue item)
        {
            if (Kind != JsonKind.Array)
                throw Wrong(JsonKind.Array);
            _items.Add(item ?? NullValue);
            return this;
        }

        /// <summary>Adds or replaces a member; insertion order is kept for writing.</summary>
        public JsonValue Set(string name, JsonValue value)
        {
            if (Kind != JsonKind.Object)
                throw Wrong(JsonKind.Object);
            if (name == null)
                throw new ArgumentNullException(nameof(name));
            value = value ?? NullValue;
            for (var index = 0; index < _members.Count; index++)
            {
                if (_members[index].Key == name)
                {
                    _members[index] = new KeyValuePair<string, JsonValue>(name, value);
                    return this;
                }
            }
            _members.Add(new KeyValuePair<string, JsonValue>(name, value));
            return this;
        }

        public bool TryGet(string name, out JsonValue value)
        {
            if (Kind != JsonKind.Object)
                throw Wrong(JsonKind.Object);
            for (var index = 0; index < _members.Count; index++)
            {
                if (_members[index].Key == name)
                {
                    value = _members[index].Value;
                    return true;
                }
            }
            value = null;
            return false;
        }

        /// <summary>Structural equality; numbers compare by value and object members ignore order.</summary>
        public bool DeepEquals(JsonValue other)
        {
            if (other == null || other.Kind != Kind)
                return false;
            switch (Kind)
            {
                case JsonKind.Null: return true;
                case JsonKind.Bool: return _bool == other._bool;
                case JsonKind.Number: return _number == other._number;
                case JsonKind.String: return string.Equals(_string, other._string, StringComparison.Ordinal);
                case JsonKind.Array:
                    if (_items.Count != other._items.Count)
                        return false;
                    for (var index = 0; index < _items.Count; index++)
                    {
                        if (!_items[index].DeepEquals(other._items[index]))
                            return false;
                    }
                    return true;
                default:
                    if (_members.Count != other._members.Count)
                        return false;
                    for (var index = 0; index < _members.Count; index++)
                    {
                        if (!other.TryGet(_members[index].Key, out var counterpart) || !_members[index].Value.DeepEquals(counterpart))
                            return false;
                    }
                    return true;
            }
        }

        public static JsonValue Parse(string text)
        {
            if (text == null)
                throw new ArgumentNullException(nameof(text));
            return new JsonParser(text).ParseDocument();
        }

        public override string ToString() => Write(false);

        public string Write(bool indented)
        {
            var builder = new StringBuilder();
            Write(builder, indented, 0);
            return builder.ToString();
        }

        void Write(StringBuilder builder, bool indented, int depth)
        {
            switch (Kind)
            {
                case JsonKind.Null: builder.Append("null"); break;
                case JsonKind.Bool: builder.Append(_bool ? "true" : "false"); break;
                case JsonKind.Number: builder.Append(FormatNumber(_number)); break;
                case JsonKind.String: WriteString(builder, _string); break;
                case JsonKind.Array:
                    builder.Append('[');
                    for (var index = 0; index < _items.Count; index++)
                    {
                        if (index > 0)
                            builder.Append(',');
                        NewLine(builder, indented, depth + 1);
                        _items[index].Write(builder, indented, depth + 1);
                    }
                    if (_items.Count > 0)
                        NewLine(builder, indented, depth);
                    builder.Append(']');
                    break;
                default:
                    builder.Append('{');
                    for (var index = 0; index < _members.Count; index++)
                    {
                        if (index > 0)
                            builder.Append(',');
                        NewLine(builder, indented, depth + 1);
                        WriteString(builder, _members[index].Key);
                        builder.Append(indented ? ": " : ":");
                        _members[index].Value.Write(builder, indented, depth + 1);
                    }
                    if (_members.Count > 0)
                        NewLine(builder, indented, depth);
                    builder.Append('}');
                    break;
            }
        }

        static void NewLine(StringBuilder builder, bool indented, int depth)
        {
            if (!indented)
                return;
            builder.Append('\n');
            builder.Append(' ', depth * 2);
        }

        static string FormatNumber(double value)
        {
            if (value == Math.Floor(value) && Math.Abs(value) < 1e15)
                return ((long)value).ToString(CultureInfo.InvariantCulture);
            return value.ToString("R", CultureInfo.InvariantCulture);
        }

        static void WriteString(StringBuilder builder, string value)
        {
            builder.Append('"');
            foreach (var character in value)
            {
                switch (character)
                {
                    case '"': builder.Append("\\\""); break;
                    case '\\': builder.Append("\\\\"); break;
                    case '\n': builder.Append("\\n"); break;
                    case '\r': builder.Append("\\r"); break;
                    case '\t': builder.Append("\\t"); break;
                    case '\b': builder.Append("\\b"); break;
                    case '\f': builder.Append("\\f"); break;
                    default:
                        // U+0085, U+2028 and U+2029 are legal in JSON but split lines in some readers; events.jsonl is read by line.
                        if (character < 0x20 || character == (char)0x85 || character == (char)0x2028 || character == (char)0x2029)
                            builder.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                        else
                            builder.Append(character);
                        break;
                }
            }
            builder.Append('"');
        }

        InvalidOperationException Wrong(JsonKind wanted) => new InvalidOperationException($"JSON value is {Kind}, not {wanted}");
    }
}
