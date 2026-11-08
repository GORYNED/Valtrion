using System;
using System.Collections.Generic;
using Valtrion.Unity.Json;

namespace Valtrion.Unity.Contracts
{
    /// <summary>A document or field does not satisfy its gdv contract. The message names the JSON path.</summary>
    public sealed class ContractException : Exception
    {
        public ContractException(string message) : base(message)
        {
        }
    }

    /// <summary>Reads one JSON object field by field, checking types, ranges and enumerations.
    /// <see cref="Done"/> rejects properties the contract does not know (closed objects); <see cref="Rest"/> hands the remaining
    /// members of an open object (step arguments) to the caller.</summary>
    sealed class ContractReader
    {
        readonly JsonValue _object;
        readonly string _path;
        readonly HashSet<string> _used = new HashSet<string>();

        public ContractReader(JsonValue value, string path)
        {
            if (value == null || value.Kind != JsonKind.Object)
                throw new ContractException($"{path}: expected an object");
            _object = value;
            _path = path;
        }

        public string Path => _path;

        public bool Has(string name) => _object.TryGet(name, out _);

        JsonValue Require(string name, JsonKind kind)
        {
            if (!_object.TryGet(name, out var value))
                throw new ContractException($"{_path}: missing required property '{name}'");
            _used.Add(name);
            if (value.Kind != kind)
                throw new ContractException($"{_path}.{name}: expected {kind}, got {value.Kind}");
            return value;
        }

        JsonValue Optional(string name, JsonKind kind)
        {
            if (!_object.TryGet(name, out var value))
                return null;
            _used.Add(name);
            if (value.Kind != kind)
                throw new ContractException($"{_path}.{name}: expected {kind}, got {value.Kind}");
            return value;
        }

        public void Const(string name, string expected)
        {
            var actual = Str(name);
            if (actual != expected)
                throw new ContractException($"{_path}.{name}: expected '{expected}', got '{actual}'");
        }

        public string Str(string name, int minLength = 1)
        {
            var value = Require(name, JsonKind.String).AsString;
            if (value.Length < minLength)
                throw new ContractException($"{_path}.{name}: shorter than {minLength}");
            return value;
        }

        public string OptStr(string name, int minLength = 0)
        {
            var value = Optional(name, JsonKind.String)?.AsString;
            if (value != null && value.Length < minLength)
                throw new ContractException($"{_path}.{name}: shorter than {minLength}");
            return value;
        }

        /// <summary>A required property whose value is a string or null.</summary>
        public string NullableStr(string name)
        {
            if (!_object.TryGet(name, out var value))
                throw new ContractException($"{_path}: missing required property '{name}'");
            _used.Add(name);
            if (value.Kind == JsonKind.Null)
                return null;
            if (value.Kind != JsonKind.String)
                throw new ContractException($"{_path}.{name}: expected string or null, got {value.Kind}");
            return value.AsString;
        }

        public string Choice(string name, string[] allowed) => Check(name, Str(name), allowed);

        public string OptChoice(string name, string[] allowed)
        {
            var value = OptStr(name);
            return value == null ? null : Check(name, value, allowed);
        }

        /// <summary>A required property that is null or one of the allowed strings.</summary>
        public string NullableChoice(string name, string[] allowed)
        {
            var value = NullableStr(name);
            return value == null ? null : Check(name, value, allowed);
        }

        string Check(string name, string value, string[] allowed)
        {
            if (Array.IndexOf(allowed, value) < 0)
                throw new ContractException($"{_path}.{name}: '{value}' is not one of {string.Join(", ", allowed)}");
            return value;
        }

        public string Identifier(string name) => CheckIdentifier(name, Str(name));

        public string OptIdentifier(string name)
        {
            var value = OptStr(name);
            return value == null ? null : CheckIdentifier(name, value);
        }

        string CheckIdentifier(string name, string value)
        {
            var valid = value.Length > 0 && value.Length <= 200 && char.IsLetterOrDigit(value[0]) && value[0] < 128;
            for (var index = 1; valid && index < value.Length; index++)
            {
                var character = value[index];
                valid = (character < 128 && char.IsLetterOrDigit(character)) || character == '.' || character == '_' || character == ':' || character == '-';
            }
            if (!valid)
                throw new ContractException($"{_path}.{name}: '{value}' is not a valid identifier");
            return value;
        }

        public string Hex(string name, int length) => CheckHex(name, Str(name), length);

        string CheckHex(string name, string value, int length)
        {
            var valid = value.Length == length;
            for (var index = 0; valid && index < value.Length; index++)
                valid = (value[index] >= '0' && value[index] <= '9') || (value[index] >= 'a' && value[index] <= 'f');
            if (!valid)
                throw new ContractException($"{_path}.{name}: expected {length} lowercase hex digits");
            return value;
        }

        public string Utc(string name)
        {
            var value = Str(name);
            if (!IsUtc(value))
                throw new ContractException($"{_path}.{name}: '{value}' is not a UTC timestamp like 2026-10-29T15:30:00Z");
            return value;
        }

        public string OptUtc(string name)
        {
            var value = OptStr(name);
            if (value != null && !IsUtc(value))
                throw new ContractException($"{_path}.{name}: '{value}' is not a UTC timestamp like 2026-10-29T15:30:00Z");
            return value;
        }

        static bool IsUtc(string value)
        {
            // yyyy-MM-ddTHH:mm:ss[.fraction]Z
            if (value.Length < 20 || value[value.Length - 1] != 'Z')
                return false;
            const string shape = "dddd-dd-ddTdd:dd:dd";
            for (var index = 0; index < shape.Length; index++)
            {
                var ok = shape[index] == 'd' ? value[index] >= '0' && value[index] <= '9' : value[index] == shape[index];
                if (!ok)
                    return false;
            }
            if (value.Length == 20)
                return true;
            if (value[19] != '.' || value.Length < 22)
                return false;
            for (var index = 20; index < value.Length - 1; index++)
            {
                if (value[index] < '0' || value[index] > '9')
                    return false;
            }
            return true;
        }

        public long Int(string name, long minimum = long.MinValue)
        {
            var value = ToInteger(name, Require(name, JsonKind.Number).AsNumber);
            return AtLeast(name, value, minimum);
        }

        public long? OptInt(string name, long minimum = long.MinValue)
        {
            var node = Optional(name, JsonKind.Number);
            return node == null ? (long?)null : AtLeast(name, ToInteger(name, node.AsNumber), minimum);
        }

        /// <summary>An integer that must fit a 32-bit int (counts, seconds, process ids); the schemas cap these at 2147483647.</summary>
        public int Int32(string name, long minimum = int.MinValue) => (int)Bounded(name, Int(name, minimum));

        public int? OptInt32(string name, long minimum = int.MinValue)
        {
            var value = OptInt(name, minimum);
            return value == null ? (int?)null : (int)Bounded(name, value.Value);
        }

        long Bounded(string name, long value)
        {
            if (value > int.MaxValue)
                throw new ContractException($"{_path}.{name}: {value} is above {int.MaxValue}");
            return value;
        }

        long ToInteger(string name, double number)
        {
            if (number != Math.Floor(number) || Math.Abs(number) > 9e15)
                throw new ContractException($"{_path}.{name}: expected an integer, got {number}");
            return (long)number;
        }

        long AtLeast(string name, long value, long minimum)
        {
            if (value < minimum)
                throw new ContractException($"{_path}.{name}: {value} is below {minimum}");
            return value;
        }

        public double Num(string name, double minimum = double.NegativeInfinity)
        {
            var value = Require(name, JsonKind.Number).AsNumber;
            if (value < minimum)
                throw new ContractException($"{_path}.{name}: {value} is below {minimum}");
            return value;
        }

        public double? OptNum(string name, double minimum = double.NegativeInfinity)
        {
            var node = Optional(name, JsonKind.Number);
            if (node == null)
                return null;
            if (node.AsNumber < minimum)
                throw new ContractException($"{_path}.{name}: {node.AsNumber} is below {minimum}");
            return node.AsNumber;
        }

        public bool Bool(string name) => Require(name, JsonKind.Bool).AsBool;

        public bool? OptBool(string name) => Optional(name, JsonKind.Bool)?.AsBool;

        public ContractReader Object(string name) => new ContractReader(Require(name, JsonKind.Object), $"{_path}.{name}");

        public ContractReader OptObject(string name)
        {
            var node = Optional(name, JsonKind.Object);
            return node == null ? null : new ContractReader(node, $"{_path}.{name}");
        }

        /// <summary>A free-form object kept as a value tree (null when absent).</summary>
        public JsonValue OptTree(string name) => Optional(name, JsonKind.Object);

        public List<string> Strings(string name) => StringsOf(Require(name, JsonKind.Array), name);

        public List<string> OptStrings(string name)
        {
            var node = Optional(name, JsonKind.Array);
            return node == null ? null : StringsOf(node, name);
        }

        List<string> StringsOf(JsonValue array, string name)
        {
            var result = new List<string>();
            for (var index = 0; index < array.Count; index++)
            {
                var item = array.Items[index];
                if (item.Kind != JsonKind.String || item.AsString.Length == 0)
                    throw new ContractException($"{_path}.{name}[{index}]: expected a non-empty string");
                result.Add(item.AsString);
            }
            return result;
        }

        public List<string> OptChoices(string name, string[] allowed, int minItems = 0)
        {
            var list = OptStrings(name);
            if (list == null)
                return null;
            if (list.Count < minItems)
                throw new ContractException($"{_path}.{name}: fewer than {minItems} items");
            foreach (var item in list)
                Check(name, item, allowed);
            return list;
        }

        public List<string> Choices(string name, string[] allowed)
        {
            var list = Strings(name);
            foreach (var item in list)
                Check(name, item, allowed);
            return list;
        }

        public List<T> Items<T>(string name, Func<ContractReader, T> read, int minItems = 0)
        {
            var array = Require(name, JsonKind.Array);
            return ReadList(array, name, read, minItems);
        }

        public List<T> OptItems<T>(string name, Func<ContractReader, T> read)
        {
            var array = Optional(name, JsonKind.Array);
            return array == null ? null : ReadList(array, name, read, 0);
        }

        List<T> ReadList<T>(JsonValue array, string name, Func<ContractReader, T> read, int minItems)
        {
            if (array.Count < minItems)
                throw new ContractException($"{_path}.{name}: fewer than {minItems} items");
            var result = new List<T>();
            for (var index = 0; index < array.Count; index++)
                result.Add(read(new ContractReader(array.Items[index], $"{_path}.{name}[{index}]")));
            return result;
        }

        public List<double> Numbers(string name)
        {
            var array = Require(name, JsonKind.Array);
            var result = new List<double>();
            for (var index = 0; index < array.Count; index++)
            {
                if (array.Items[index].Kind != JsonKind.Number)
                    throw new ContractException($"{_path}.{name}[{index}]: expected a number");
                result.Add(array.Items[index].AsNumber);
            }
            return result;
        }

        /// <summary>Reads a map whose values are objects (for example the metric map of a plan).</summary>
        public Dictionary<string, T> Map<T>(string name, Func<ContractReader, T> read)
        {
            var node = Require(name, JsonKind.Object);
            return ReadMap(node, name, read);
        }

        public Dictionary<string, T> OptMap<T>(string name, Func<ContractReader, T> read)
        {
            var node = Optional(name, JsonKind.Object);
            return node == null ? null : ReadMap(node, name, read);
        }

        Dictionary<string, T> ReadMap<T>(JsonValue node, string name, Func<ContractReader, T> read)
        {
            var result = new Dictionary<string, T>();
            foreach (var member in node.Members)
                result[member.Key] = read(new ContractReader(member.Value, $"{_path}.{name}.{member.Key}"));
            return result;
        }

        public Dictionary<string, double> NumberMap(string name)
        {
            var node = Optional(name, JsonKind.Object);
            if (node == null)
                return null;
            var result = new Dictionary<string, double>();
            foreach (var member in node.Members)
            {
                if (member.Value.Kind != JsonKind.Number)
                    throw new ContractException($"{_path}.{name}.{member.Key}: expected a number");
                result[member.Key] = member.Value.AsNumber;
            }
            return result;
        }

        /// <summary>A map whose values are lists of non-empty strings (null when absent).</summary>
        public Dictionary<string, List<string>> StringListMap(string name)
        {
            var node = Optional(name, JsonKind.Object);
            if (node == null)
                return null;
            var result = new Dictionary<string, List<string>>();
            foreach (var member in node.Members)
            {
                if (member.Value.Kind != JsonKind.Array)
                    throw new ContractException($"{_path}.{name}.{member.Key}: expected an array");
                result[member.Key] = StringsOf(member.Value, $"{name}.{member.Key}");
            }
            return result;
        }

        public Dictionary<string, string> StringMap(string name)
        {
            var node = Optional(name, JsonKind.Object);
            if (node == null)
                return null;
            var result = new Dictionary<string, string>();
            foreach (var member in node.Members)
            {
                if (member.Value.Kind != JsonKind.String)
                    throw new ContractException($"{_path}.{name}.{member.Key}: expected a string");
                result[member.Key] = member.Value.AsString;
            }
            return result;
        }

        /// <summary>Returns the members not read so far as a new object and marks them used (open objects).</summary>
        public JsonValue Rest()
        {
            var rest = JsonValue.NewObject();
            foreach (var member in _object.Members)
            {
                if (_used.Add(member.Key))
                    rest.Set(member.Key, member.Value);
            }
            return rest;
        }

        /// <summary>Rejects every property that was not read.</summary>
        public void Done()
        {
            foreach (var member in _object.Members)
            {
                if (!_used.Contains(member.Key))
                    throw new ContractException($"{_path}: unexpected property '{member.Key}'");
            }
        }
    }

    static class JsonBuild
    {
        public static JsonValue Str(string value) => JsonValue.From(value);

        public static JsonValue Num(double value) => JsonValue.From(value);

        public static JsonValue Strings(IEnumerable<string> values)
        {
            var array = JsonValue.NewArray();
            foreach (var value in values)
                array.Add(JsonValue.From(value));
            return array;
        }

        public static JsonValue Numbers(IEnumerable<double> values)
        {
            var array = JsonValue.NewArray();
            foreach (var value in values)
                array.Add(JsonValue.From(value));
            return array;
        }

        public static JsonValue Array<T>(IEnumerable<T> values, Func<T, JsonValue> write)
        {
            var array = JsonValue.NewArray();
            foreach (var value in values)
                array.Add(write(value));
            return array;
        }

        public static JsonValue Map<T>(IDictionary<string, T> values, Func<T, JsonValue> write)
        {
            var map = JsonValue.NewObject();
            foreach (var pair in values)
                map.Set(pair.Key, write(pair.Value));
            return map;
        }

        public static JsonValue NumberMap(IDictionary<string, double> values) => Map(values, JsonValue.From);

        public static JsonValue StringMap(IDictionary<string, string> values) => Map(values, JsonValue.From);

        /// <summary>Sets a member only when the value is present.</summary>
        public static JsonValue SetIf(this JsonValue target, string name, JsonValue value)
        {
            if (value != null)
                target.Set(name, value);
            return target;
        }

        public static JsonValue SetIf(this JsonValue target, string name, string value) =>
            value == null ? target : target.Set(name, JsonValue.From(value));

        public static JsonValue SetIf(this JsonValue target, string name, long? value) =>
            value == null ? target : target.Set(name, JsonValue.From(value.Value));

        public static JsonValue SetIf(this JsonValue target, string name, double? value) =>
            value == null ? target : target.Set(name, JsonValue.From(value.Value));

        public static JsonValue SetIf(this JsonValue target, string name, bool? value) =>
            value == null ? target : target.Set(name, JsonValue.From(value.Value));
    }
}
