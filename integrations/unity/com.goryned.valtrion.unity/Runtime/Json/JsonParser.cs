using System;
using System.Globalization;
using System.Text;

namespace Valtrion.Unity.Json
{
    /// <summary>Thrown for malformed JSON text; carries the character offset.</summary>
    public sealed class JsonException : Exception
    {
        public JsonException(string message, int offset) : base($"{message} at offset {offset}")
        {
            Offset = offset;
        }

        public int Offset { get; }
    }

    /// <summary>Strict recursive-descent JSON reader (RFC 8259): no comments, no trailing commas, no duplicate keys.</summary>
    sealed class JsonParser
    {
        const int MaxDepth = 64;

        readonly string _text;
        int _position;

        public JsonParser(string text)
        {
            _text = text;
        }

        public JsonValue ParseDocument()
        {
            // A byte-order mark is a file-encoding matter: readers such as File.ReadAllText drop it, so the parser never sees one.
            var value = ParseValue(0);
            SkipWhitespace();
            if (_position != _text.Length)
                throw Error("unexpected content after the document");
            return value;
        }

        JsonValue ParseValue(int depth)
        {
            SkipWhitespace();
            if (_position >= _text.Length)
                throw Error("unexpected end of document");
            switch (_text[_position])
            {
                case '{': return ParseObject(depth);
                case '[': return ParseArray(depth);
                case '"': return JsonValue.From(ParseString());
                case 't': ExpectWord("true"); return JsonValue.From(true);
                case 'f': ExpectWord("false"); return JsonValue.From(false);
                case 'n': ExpectWord("null"); return JsonValue.Null;
                default: return ParseNumber();
            }
        }

        JsonValue ParseObject(int depth)
        {
            if (depth >= MaxDepth)
                throw Error("document is nested too deeply");
            var result = JsonValue.NewObject();
            _position++;
            SkipWhitespace();
            if (Peek() == '}')
            {
                _position++;
                return result;
            }
            while (true)
            {
                SkipWhitespace();
                if (Peek() != '"')
                    throw Error("expected a property name");
                var name = ParseString();
                if (result.TryGet(name, out _))
                    throw Error($"duplicate property '{name}'");
                SkipWhitespace();
                if (Peek() != ':')
                    throw Error("expected ':'");
                _position++;
                result.Set(name, ParseValue(depth + 1));
                SkipWhitespace();
                var next = Peek();
                _position++;
                if (next == '}')
                    return result;
                if (next != ',')
                    throw Error("expected ',' or '}'");
            }
        }

        JsonValue ParseArray(int depth)
        {
            if (depth >= MaxDepth)
                throw Error("document is nested too deeply");
            var result = JsonValue.NewArray();
            _position++;
            SkipWhitespace();
            if (Peek() == ']')
            {
                _position++;
                return result;
            }
            while (true)
            {
                result.Add(ParseValue(depth + 1));
                SkipWhitespace();
                var next = Peek();
                _position++;
                if (next == ']')
                    return result;
                if (next != ',')
                    throw Error("expected ',' or ']'");
            }
        }

        string ParseString()
        {
            _position++;
            var builder = new StringBuilder();
            while (true)
            {
                if (_position >= _text.Length)
                    throw Error("unterminated string");
                var character = _text[_position++];
                if (character == '"')
                    return builder.ToString();
                if (character < 0x20)
                    throw Error("control character in string");
                if (character != '\\')
                {
                    builder.Append(character);
                    continue;
                }
                if (_position >= _text.Length)
                    throw Error("unterminated escape");
                var escape = _text[_position++];
                switch (escape)
                {
                    case '"': builder.Append('"'); break;
                    case '\\': builder.Append('\\'); break;
                    case '/': builder.Append('/'); break;
                    case 'b': builder.Append('\b'); break;
                    case 'f': builder.Append('\f'); break;
                    case 'n': builder.Append('\n'); break;
                    case 'r': builder.Append('\r'); break;
                    case 't': builder.Append('\t'); break;
                    case 'u':
                        if (_position + 4 > _text.Length)
                            throw Error("invalid \\u escape");
                        var code = 0;
                        for (var digit = 0; digit < 4; digit++)
                        {
                            var value = HexValue(_text[_position + digit]);
                            if (value < 0)
                                throw Error("invalid \\u escape");
                            code = code * 16 + value;
                        }
                        builder.Append((char)code);
                        _position += 4;
                        break;
                    default: throw Error("invalid escape");
                }
            }
        }

        JsonValue ParseNumber()
        {
            var start = _position;
            if (Peek() == '-')
                _position++;
            if (Peek() == '0')
                _position++;
            else if (Peek() >= '1' && Peek() <= '9')
                SkipDigits();
            else
                throw Error("invalid value");
            if (Peek() == '.')
            {
                _position++;
                if (!IsDigit(Peek()))
                    throw Error("digits expected after '.'");
                SkipDigits();
            }
            if (Peek() == 'e' || Peek() == 'E')
            {
                _position++;
                if (Peek() == '+' || Peek() == '-')
                    _position++;
                if (!IsDigit(Peek()))
                    throw Error("digits expected in exponent");
                SkipDigits();
            }
            double number;
            try
            {
                number = double.Parse(_text.Substring(start, _position - start), NumberStyles.Float, CultureInfo.InvariantCulture);
            }
            catch (OverflowException)
            {
                throw Error("number out of range");
            }
            if (double.IsInfinity(number) || double.IsNaN(number))
                throw Error("number out of range");
            return JsonValue.From(number);
        }

        void SkipDigits()
        {
            while (IsDigit(Peek()))
                _position++;
        }

        static bool IsDigit(char character) => character >= '0' && character <= '9';

        static int HexValue(char character)
        {
            if (character >= '0' && character <= '9')
                return character - '0';
            if (character >= 'a' && character <= 'f')
                return character - 'a' + 10;
            if (character >= 'A' && character <= 'F')
                return character - 'A' + 10;
            return -1;
        }

        char Peek() => _position < _text.Length ? _text[_position] : '\0';

        void SkipWhitespace()
        {
            while (_position < _text.Length && (_text[_position] == ' ' || _text[_position] == '\t' || _text[_position] == '\n' || _text[_position] == '\r'))
                _position++;
        }

        void ExpectWord(string word)
        {
            if (string.CompareOrdinal(_text, _position, word, 0, word.Length) != 0)
                throw Error("invalid value");
            _position += word.Length;
        }

        JsonException Error(string message) => new JsonException(message, _position);
    }
}
