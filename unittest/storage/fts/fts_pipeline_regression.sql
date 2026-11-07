-- Dedicated regression database; rebuilds only codex_fts_pipeline.
SET NAMES utf8mb4;
SET SESSION sql_mode='';
SET ob_query_timeout=60000000;
DROP DATABASE IF EXISTS codex_fts_pipeline;
CREATE DATABASE codex_fts_pipeline;
USE codex_fts_pipeline;
CREATE TABLE docs(id INT PRIMARY KEY, body TEXT);
INSERT INTO docs VALUES
(0, ''),
(1, ' '),
(2, '数据库全文索引 中文英文混合 OceanBase123 tokenizer'),
(3, '南京市长江大桥 研究生命起源 中华人民共和国'),
(4, '一百二十三个 123公斤 三千万元 二〇二六年12月31日 12.34米'),
(5, 'ＡＢＣ１２３ AＢＣ123 abc-123 test@example.com C++'),
(6, 'あいうえお 한글 😀𠀀 𝔘nicode'),
(7, 'English UPPER lower MiXeD the AND audit audit'),
(8, '，。！？ -- ++ @ # _ 1.23.45'),
(9, _utf8mb4 0x41004209430d0a44),
(10, REPEAT('数据库 全文索引 OceanBase 123公斤 南京市长江大桥 😀\n', 30)),
(11, REPEAT('数据库 全文索引 OceanBase 123公斤 南京市长江大桥 😀\n', 70)),
(12, REPEAT('数据库 全文索引 OceanBase 123公斤 南京市长江大桥 😀\n', 120)),
(13, '全文索引南京市三百二十个English数据库한글 Tokenizer数据库数据库长江大桥ＡＢＣ１２３Tokenizer三百二十个123.45kgあいう三百二十个한글数据库English数据库南京市\nEnglishＡＢＣ１２３南京市😀，，南京市123.45kg，南京市あいう123.45kg ，ＡＢＣ１２３\tEnglish한글全文索引ＡＢＣ１２３全文索引\t全文索引三百二十个ＡＢＣ１２３😀，长江大桥\nあいうEnglish数据库 全文索引南京市😀\t数据库ＡＢＣ１２３南京市\nＡＢＣ１２３あいう数据库\nTokenizer😀😀😀あいう数据库 \tＡＢＣ１２３长江大桥Tokenizerあいう\n😀\n😀数据库\t 😀'),
(14, '😀English😀南京市全文索引😀あいう\t \t\n\t ，三百二十个EnglishＡＢＣ１２３，'),
(15, '한글，123.45kg한글三百二十个😀南京市TokenizerEnglishEnglish数据库한글123.45kg长江大桥全文索引数据库南京市😀😀\tあいうあいう三百二十个😀\t\n\nTokenizer全文索引，\n三百二十个\nTokenizer，한글\tＡＢＣ１２３123.45kg，\t123.45kgあいうTokenizer😀\n长江大桥한글三百二十个😀全文索引한글あいう😀한글あいう长江大桥あいうEnglish三百二十个ＡＢＣ１２３南京市\n😀あいう三百二十个三百二十个长江大桥あいう南京市数据库あいうEnglish\t南京市😀 한글123.45kg三百二十个全文索引Tokenizer\t长江大桥长江大桥English\n123.45kgEnglishEnglish123.45kg😀\t😀\t，English\tあいう'),
(16, ' English 三百二十个  😀三百二十个ＡＢＣ１２３，Tokenizer😀 Tokenizer\n\t长江大桥한글😀三百二十个三百二十个\t数据库 あいう数据库ＡＢＣ１２３， 长江大桥あいうTokenizer南京市全文索引😀123.45kg长江大桥😀한글三百二十个\t123.45kg😀한글ＡＢＣ１２３\t长江大桥三百二十个\n全文索引，Tokenizer长江大桥，ＡＢＣ１２３😀Tokenizer，English 南京市123.45kg123.45kg南京市\n数据库长江大桥😀😀 あいうあいう\t，全文索引\n，长江大桥\t数据库\n\nＡＢＣ１２３数据库TokenizerあいうEnglishEnglishTokenizerあいうあいう한글南京市123.45kgEnglish한글あいう数据库'),
(17, '\t 全文索引Tokenizer\n\n😀\t长江大桥 \n\tあいう南京市长江大桥123.45kg\n😀\nEnglish南京市长江大桥123.45kgあいう \t123.45kgEnglish\n数据库123.45kg南京市😀数据库三百二十个English한글😀，数据库数据库长江大桥，\t南京市한글'),
(18, 'ＡＢＣ１２３长江大桥数据库\n😀数据库English Tokenizer长江大桥Tokenizer😀\t，あいう长江大桥English한글\n\t三百二十个三百二十个\n南京市English，南京市あいうＡＢＣ１２３南京市Tokenizer长江大桥 English\t数据库한글Tokenizer南京市南京市123.45kg Tokenizer数据库三百二十个长江大桥长江大桥Tokenizer123.45kgTokenizer\t한글😀Tokenizer\tEnglish한글😀 😀\nEnglish南京市\n EnglishTokenizer三百二十个'),
(19, '南京市  三百二十个あいう123.45kg数据库😀TokenizerTokenizer😀，ＡＢＣ１２３\n全文索引123.45kg长江大桥长江大桥长江大桥한글😀123.45kgあいう\t数据库\nＡＢＣ１２３三百二十个 长江大桥 😀한글数据库😀English\n全文索引Tokenizer한글 123.45kg南京市ＡＢＣ１２３Tokenizer数据库'),
(20, '南京市123.45kg三百二十个\t全文索引全文索引数据库😀南京市南京市长江大桥あいう南京市全文索引\t长江大桥三百二十个😀Tokenizer123.45kgTokenizer\t\t全文索引，长江大桥😀\tEnglish\nあいう😀，，全文索引\t，ＡＢＣ１２３数据库\n三百二十个English南京市あいうTokenizer三百二十个Tokenizer123.45kg，长江大桥😀三百二十个😀한글EnglishＡＢＣ１２３Tokenizer，数据库ＡＢＣ１２３全文索引ＡＢＣ１２３ＡＢＣ１２３\n三百二十个全文索引长江大桥南京市，\n，Tokenizer 三百二十个三百二十个全文索引全文索引三百二十个三百二十个한글Englishあいう数据库三百二十个ＡＢＣ１２３数据库三百二十个南京市한글Tokenizer 123.45kg，한글南京市三百二十个三百二十个南京市'),
(21, 'English한글English数据库한글数据库 Tokenizer南京市，三百二十个ＡＢＣ１２３\n南京市长江大桥，\t数据库南京市Tokenizer，南京市😀 あいう\n长江大桥ＡＢＣ１２３123.45kg数据库😀あいう三百二十个 \t\t\n全文索引ＡＢＣ１２３Tokenizer\t\t全文索引English三百二十个あいう数据库ＡＢＣ１２３한글南京市English数据库😀全文索引三百二十个 ＡＢＣ１２３'),
(22, ' \nEnglish，EnglishEnglishTokenizer\t数据库TokenizerＡＢＣ１２３English😀한글\t123.45kg\tEnglish，ＡＢＣ１２３あいうＡＢＣ１２３南京市😀全文索引😀南京市 あいう123.45kg123.45kgＡＢＣ１２３English南京市😀한글あいう😀English\t三百二十个\tTokenizerあいう한글EnglishEnglish😀😀三百二十个ＡＢＣ１２３数据库English三百二十个ＡＢＣ１２３三百二十个ＡＢＣ１２３\tあいう한글，あいう\tEnglish南京市😀长江大桥三百二十个\t'),
(23, '，，数据库한글あいう😀\n长江大桥全文索引\n123.45kg\t长江大桥三百二十个全文索引\n全文索引ＡＢＣ１２３ＡＢＣ１２３English，😀😀长江大桥\tあいうEnglish\t 长江大桥 한글 English南京市 三百二十个三百二十个TokenizerTokenizer123.45kg，南京市数据库123.45kg全文索引Tokenizer三百二十个\t😀😀Tokenizer😀南京市，ＡＢＣ１２３123.45kgTokenizer한글\n南京市\n'),
(24, 'あいう\n😀\nあいう123.45kg 三百二十个三百二十个数据库长江大桥😀\tあいう长江大桥全文索引数据库Tokenizerあいう😀全文索引😀，あいう한글，😀长江大桥'),
(25, ' 长江大桥数据库Tokenizer\n\n长江大桥Tokenizer123.45kg南京市，，\t123.45kg한글全文索引あいう😀ＡＢＣ１２３全文索引南京市数据库南京市TokenizerＡＢＣ１２３EnglishＡＢＣ１２３ 数据库ＡＢＣ１２３😀，😀全文索引南京市한글三百二十个😀全文索引Tokenizer三百二十个123.45kg\n长江大桥长江大桥123.45kg123.45kgＡＢＣ１２３한글，English😀三百二十个 あいう长江大桥 English三百二十个\t，한글한글ＡＢＣ１２３'),
(26, '\nTokenizer\nＡＢＣ１２３，南京市\n全文索引 南京市'),
(27, '😀，数据库，数据库한글ＡＢＣ１２３，あいう ＡＢＣ１２３123.45kg123.45kgEnglish'),
(28, '长江大桥，한글长江大桥あいうTokenizer😀南京市长江大桥English\nＡＢＣ１２３，\nＡＢＣ１２３，123.45kg，，南京市Tokenizer全文索引あいう全文索引，\n\t三百二十个EnglishEnglishあいう😀Tokenizer한글123.45kg123.45kg三百二十个长江大桥English한글123.45kg\tEnglishＡＢＣ１２３Tokenizer\tTokenizerEnglishあいうTokenizer数据库あいうTokenizerTokenizer长江大桥\t'),
(29, '\n\t\t😀Tokenizer한글，123.45kg English あいうあいうTokenizer，あいう한글English三百二十个123.45kg，😀，한글한글 EnglishTokenizer全文索引\nEnglish三百二十个\t한글南京市南京市'),
(30, '全文索引EnglishEnglish三百二十个\n三百二十个\n三百二十个😀全文索引\nTokenizerTokenizer😀\t全文索引ＡＢＣ１２３\t，全文索引123.45kg123.45kg全文索引한글，，あいう あいう한글全文索引长江大桥全文索引あいう，数据库😀全文索引\tEnglish数据库三百二十个数据库123.45kg数据库あいう长江大桥数据库😀\t123.45kg数据库😀\n南京市Tokenizer  \tＡＢＣ１２３한글あいうTokenizerEnglish \nEnglish\t😀123.45kg한글123.45kg😀あいう南京市\n 数据库数据库全文索引한글长江大桥ＡＢＣ１２３全文索引한글  123.45kg，123.45kg'),
(31, '123.45kg南京市한글한글English三百二十个\t，\n三百二十个全文索引三百二十个😀😀😀😀ＡＢＣ１２３长江大桥 ＡＢＣ１２３长江大桥\n长江大桥，あいう'),
(32, '数据库한글长江大桥 长江大桥한글三百二十个Tokenizer한글TokenizerEnglish\n南京市ＡＢＣ１２３长江大桥\tTokenizer 数据库English三百二十个长江大桥 数据库Tokenizer😀123.45kg数据库长江大桥Tokenizer\t\t한글\n全文索引\n全文索引三百二十个ＡＢＣ１２３\t\nあいうEnglish😀南京市Tokenizer\tTokenizerＡＢＣ１２３\t 三百二十个Tokenizer\t 数据库あいう123.45kg南京市，全文索引한글あいう\nＡＢＣ１２３ＡＢＣ１２３ＡＢＣ１２３长江大桥😀'),
(33, 'English南京市南京市数据库English，EnglishＡＢＣ１２３长江大桥123.45kg\t全文索引\n한글ＡＢＣ１２３三百二十个南京市English数据库长江大桥长江大桥한글Tokenizer全文索引EnglishEnglish한글あいう，한글あいうＡＢＣ１２３ 数据库三百二十个ＡＢＣ１２３한글123.45kg\n全文索引三百二十个ＡＢＣ１２３长江大桥，\n😀ＡＢＣ１２３123.45kg数据库 Tokenizer한글한글数据库\t\n，全文索引 한글数据库'),
(34, '한글数据库\tＡＢＣ１２３数据库\n， 南京市 Tokenizer한글三百二十个 あいうTokenizer全文索引全文索引\n123.45kg\t😀南京市あいう😀Tokenizer南京市TokenizerEnglish\n全文索引长江大桥 南京市全文索引English三百二十个长江大桥 한글😀Tokenizer123.45kg长江大桥English南京市长江大桥数据库한글数据库全文索引\n😀三百二十个\tあいう'),
(35, '全文索引数据库한글南京市全文索引数据库123.45kg全文索引English全文索引123.45kgあいう，全文索引123.45kg全文索引EnglishＡＢＣ１２３'),
(36, ' \t数据库Tokenizer数据库ＡＢＣ１２３三百二十个，南京市，ＡＢＣ１２３한글长江大桥あいう\t全文索引 数据库English长江大桥123.45kg😀三百二十个ＡＢＣ１２３ \n \n，，123.45kg123.45kg全文索引 ，，한글长江大桥全文索引한글全文索引あいう全文索引123.45kgTokenizer Tokenizer全文索引三百二十个ＡＢＣ１２３数据库\t南京市\tEnglish 南京市长江大桥\t长江大桥한글长江大桥，EnglishTokenizer\nTokenizer  三百二十个全文索引，三百二十个あいう数据库数据库😀\t全文索引\t，English南京市数据库한글😀\tＡＢＣ１２３123.45kg长江大桥😀，，'),
(37, 'English全文索引三百二十个数据库123.45kgＡＢＣ１２３Englishあいう123.45kgＡＢＣ１２３EnglishあいうＡＢＣ１２３😀，あいう长江大桥あいうTokenizer数据库123.45kg全文索引한글 三百二十个全文索引，\t长江大桥EnglishEnglishあいう数据库English한글 English南京市ＡＢＣ１２３，'),
(38, '南京市南京市ＡＢＣ１２３ＡＢＣ１２３한글한글数据库한글长江大桥南京市Tokenizer\n全文索引 ＡＢＣ１２３English한글南京市，，😀，한글😀三百二十个ＡＢＣ１２３，あいう😀\n南京市😀ＡＢＣ１２３全文索引 あいう\tEnglishＡＢＣ１２３Tokenizerあいう数据库全文索引'),
(39, 'ＡＢＣ１２３Tokenizer한글\n\n한글123.45kgあいうＡＢＣ１２３😀😀 😀ＡＢＣ１２３한글Tokenizer南京市English数据库ＡＢＣ１２３长江大桥南京市\n😀あいう😀数据库，\n数据库Tokenizer\n，\t全文索引\n\n😀😀ＡＢＣ１２３ＡＢＣ１２３ＡＢＣ１２３ＡＢＣ１２３123.45kg한글数据库长江大桥'),
(40, 'Tokenizer三百二十个\t😀ＡＢＣ１２３三百二十个ＡＢＣ１２３ 123.45kgあいう数据库English\nEnglish三百二十个123.45kg数据库Tokenizer\tあいう\t\n数据库あいう한글 全文索引English\t全文索引English😀한글あいう， 南京市\t数据库한글数据库南京市EnglishEnglish全文索引あいうあいうTokenizer全文索引English全文索引三百二十个\t全文索引Tokenizer数据库한글ＡＢＣ１２３あいう あいう😀'),
(41, '😀三百二十个😀\n长江大桥，English😀\n全文索引한글あいうあいう あいう长江大桥EnglishTokenizer\t😀ＡＢＣ１２３，ＡＢＣ１２３ ，한글南京市全文索引한글TokenizerＡＢＣ１２３\n\t\n，，ＡＢＣ１２３\n123.45kgＡＢＣ１２３ 한글😀，数据库\t한글😀English全文索引😀长江大桥ＡＢＣ１２３全文索引南京市，全文索引あいうEnglishTokenizerあいう'),
(42, '数据库长江大桥长江大桥\t长江大桥长江大桥English'),
(43, '三百二十个한글三百二十个，长江大桥南京市Tokenizer数据库三百二十个长江大桥Tokenizer😀English，长江大桥😀'),
(44, 'Tokenizer，三百二十个南京市한글123.45kgＡＢＣ１２３한글あいうＡＢＣ１２３ 长江大桥TokenizerTokenizer，EnglishＡＢＣ１２３한글EnglishEnglish123.45kg한글😀\t123.45kg한글长江大桥EnglishＡＢＣ１２３全文索引あいう南京市ＡＢＣ１２３三百二十个\t  English长江大桥\n三百二十个数据库\nＡＢＣ１２３长江大桥123.45kg😀三百二十个\t 😀，English\n，\n全文索引三百二十个Tokenizer 三百二十个，\t한글Tokenizer\t \n三百二十个あいうＡＢＣ１２３\t三百二十个\t\tEnglishあいう123.45kg南京市\t\t ，南京市南京市あいう，，한글 '),
(45, '长江大桥😀三百二十个\n南京市，长江大桥123.45kg한글，Tokenizer\nあいう全文索引   한글南京市😀 三百二十个\tＡＢＣ１２３😀Tokenizerあいう한글English三百二十个123.45kg한글数据库，😀，TokenizerEnglish\t \nEnglishTokenizer三百二十个全文索引三百二十个あいう南京市あいう三百二十个\n😀 全文索引😀😀南京市한글あいうＡＢＣ１２３数据库あいうＡＢＣ１２３😀ＡＢＣ１２３Englishあいう全文索引， ＡＢＣ１２３\n\n\n 数据库English\n长江大桥数据库 😀南京市南京市长江大桥三百二十个 \tTokenizer全文索引三百二十个，ＡＢＣ１２３한글Tokenizer'),
(46, '한글长江大桥😀长江大桥南京市ＡＢＣ１２３全文索引あいう全文索引南京市\n한글全文索引\tTokenizer\n南京市123.45kg\t，한글数据库数据库한글 123.45kg123.45kg长江大桥あいう南京市\tあいう😀 ＡＢＣ１２３EnglishＡＢＣ１２３三百二十个三百二十个数据库 ＡＢＣ１２３，全文索引三百二十个あいう😀한글数据库，数据库123.45kgあいう三百二十个 한글ＡＢＣ１２３한글长江大桥三百二十个三百二十个\t123.45kg数据库 😀😀，😀，全文索引123.45kg\t\t三百二十个 😀 长江大桥😀全文索引😀'),
(47, 'Tokenizerあいう长江大桥 全文索引😀English123.45kg三百二十个ＡＢＣ１２３\n南京市数据库あいう\t全文索引\nEnglish'),
(48, 'Englishあいう123.45kg数据库English长江大桥三百二十个\t全文索引\t长江大桥数据库Tokenizer全文索引南京市\n南京市\n三百二十个，😀Tokenizer\nあいう长江大桥Tokenizer 三百二十个ＡＢＣ１２３全文索引あいう长江大桥南京市南京市あいう长江大桥长江大桥123.45kg\n123.45kg😀ＡＢＣ１２３한글あいう数据库'),
(49, '数据库，Tokenizer南京市三百二十个\n\n，全文索引数据库三百二十个三百二十个数据库南京市南京市\t南京市あいう😀\n123.45kg南京市English全文索引长江大桥\n长江大桥全文索引あいう，数据库，ＡＢＣ１２３あいう 南京市 南京市123.45kgあいうあいう全文索引😀\tTokenizer数据库😀한글あいう😀长江大桥长江大桥长江大桥EnglishTokenizer한글123.45kg，English\n，あいう あいう123.45kgＡＢＣ１２３全文索引  全文索引123.45kg'),
(50, '南京市😀三百二十个南京市ＡＢＣ１２３ あいう123.45kgEnglish한글，全文索引 ＡＢＣ１２３，ＡＢＣ１２３， あいうあいう数据库English数据库\n😀 \n，，'),
(51, '123.45kg三百二十个，全文索引あいう'),
(52, '数据库Tokenizer全文索引한글한글数据库한글三百二十个123.45kg数据库한글'),
(53, '，English数据库三百二十个あいう长江大桥ＡＢＣ１２３ＡＢＣ１２３数据库全文索引EnglishEnglishTokenizer\t南京市\t三百二十个全文索引😀TokenizerＡＢＣ１２３123.45kg 南京市한글三百二十个 Tokenizer\t全文索引123.45kg\n123.45kg\n长江大桥 ＡＢＣ１２３数据库三百二十个全文索引长江大桥  ，'),
(54, '123.45kgEnglishEnglish三百二十个ＡＢＣ１２３全文索引Tokenizer三百二十个三百二十个全文索引\n，한글\t123.45kg123.45kg😀123.45kg😀三百二十个 全文索引\t全文索引😀English长江大桥数据库长江大桥\t，数据库\n南京市\t123.45kg数据库三百二十个长江大桥数据库English，123.45kg한글全文索引数据库한글English三百二十个English😀あいう\nEnglishＡＢＣ１２３Tokenizer\n全文索引\t\tTokenizerあいう三百二十个长江大桥Tokenizer南京市한글Tokenizer😀English，あいう\nTokenizer\t，123.45kgEnglish三百二十个南京市三百二十个，한글三百二十个南京市😀Tokenizer数据库\n\nEnglish全文索引南京市数据库 あいう'),
(55, '， 三百二十个\n长江大桥， 123.45kgEnglish😀三百二十个English南京市，\tあいうEnglish，三百二十个三百二十个😀😀全文索引\n三百二十个三百二十个123.45kgＡＢＣ１２３ 😀ＡＢＣ１２３😀 全文索引 南京市 长江大桥数据库\n三百二十个三百二十个TokenizerＡＢＣ１２３全文索引'),
(56, '😀한글，あいうあいう123.45kg123.45kgあいう三百二十个 '),
(57, '😀\n😀\t한글\nあいう全文索引全文索引😀\t한글南京市English，\n南京市あいう南京市 Tokenizer全文索引English全文索引\n\n한글数据库123.45kg，三百二十个😀あいうEnglish😀数据库😀长江大桥English南京市\n三百二十个三百二十个全文索引123.45kg\nあいう，三百二十个 数据库😀Tokenizer123.45kg123.45kgＡＢＣ１２３\t'),
(58, '南京市全文索引한글，Tokenizerあいう\n한글长江大桥，三百二十个数据库全文索引\n数据库南京市，English123.45kgＡＢＣ１２３\tＡＢＣ１２３全文索引，全文索引EnglishあいうTokenizer数据库EnglishＡＢＣ１２３\nTokenizer\n，English한글长江大桥😀 \nあいう あいう南京市\nＡＢＣ１２３😀\tあいうＡＢＣ１２３南京市EnglishTokenizer한글あいう Tokenizer数据库ＡＢＣ１２３ '),
(59, '123.45kgTokenizer\t123.45kg123.45kgEnglish \tTokenizer한글南京市Tokenizer全文索引ＡＢＣ１２３ ＡＢＣ１２３ＡＢＣ１２３\tTokenizer长江大桥，한글南京市长江大桥全文索引三百二十个三百二十个数据库한글\n😀全文索引😀English\t，あいうTokenizer数据库 Tokenizer全文索引한글全文索引😀한글ＡＢＣ１２３全文索引😀English\n'),
(60, 'あいう\n あいう三百二十个\nTokenizer\n\t，\nあいう全文索引\t\nTokenizerTokenizer'),
(61, 'English数据库😀123.45kg三百二十个Tokenizer全文索引Englishあいう한글ＡＢＣ１２３南京市123.45kg\t南京市  😀ＡＢＣ１２３南京市😀123.45kg南京市长江大桥Tokenizer全文索引长江大桥 ，\n \t \nTokenizer 三百二十个😀全文索引数据库，数据库，한글南京市😀数据库数据库😀\t\nあいうＡＢＣ１２３数据库한글Tokenizer\n123.45kgTokenizerＡＢＣ１２３数据库\t，三百二十个123.45kg한글Tokenizer南京市😀Tokenizer😀Tokenizer三百二十个三百二十个长江大桥数据库南京市123.45kg한글全文索引Tokenizerあいう长江大桥南京市123.45kg全文索引ＡＢＣ１２３三百二十个English\n\tEnglish，'),
(62, '，Tokenizer\n\t三百二十个南京市'),
(63, '数据库数据库 全文索引123.45kg\n南京市数据库\t，Tokenizer123.45kgEnglishTokenizer한글，三百二十个한글123.45kg😀南京市English南京市ＡＢＣ１２３あいうＡＢＣ１２３😀\t\tTokenizer数据库 \n😀，三百二十个あいう，南京市Tokenizer数据库三百二十个English한글Tokenizerあいう\n，\n长江大桥全文索引한글数据库长江大桥ＡＢＣ１２３数据库\nあいう，😀😀Tokenizer三百二十个数据库 한글\t ，全文索引Tokenizer한글长江大桥123.45kg\n😀三百二十个😀한글あいう😀한글南京市三百二十个，English'),
(64, '한글Tokenizer\t😀全文索引 EnglishTokenizer ＡＢＣ１２３\t😀\t南京市 长江大桥\t😀ＡＢＣ１２３全文索引\t\n123.45kg 长江大桥English，三百二十个123.45kg长江大桥数据库\nTokenizer\tＡＢＣ１２３123.45kgあいう123.45kg한글あいう한글ＡＢＣ１２３全文索引\tあいう三百二十个，あいう\t长江大桥Tokenizer长江大桥南京市Tokenizer数据库全文索引あいう长江大桥全文索引😀Tokenizer English，Tokenizer全文索引😀\t长江大桥123.45kg\t123.45kg，\tEnglishEnglish ，English数据库EnglishEnglishあいう南京市あいう123.45kg全文索引\t全文索引ＡＢＣ１２３English长江大桥数据库三百二十个'),
(65, '\tあいう，한글123.45kg\n 长江大桥😀南京市，全文索引三百二十个 全文索引三百二十个 全文索引あいう \t\t 한글ＡＢＣ１２３三百二十个全文索引한글全文索引TokenizerTokenizer，\nあいう'),
(66, '， 三百二十个长江大桥数据库😀한글数据库ＡＢＣ１２３全文索引123.45kg😀English三百二十个 南京市あいうEnglish ，三百二十个\t南京市全文索引TokenizerTokenizer \t한글あいう\t\nＡＢＣ１２３数据库한글，，あいう123.45kg한글，Tokenizer\tあいうＡＢＣ１２３，ＡＢＣ１２３TokenizerEnglishあいう'),
(67, '全文索引，한글数据库한글あいう123.45kgTokenizerTokenizerＡＢＣ１２３全文索引한글あいう南京市长江大桥\tEnglish数据库😀南京市English😀123.45kg，😀\t数据库123.45kg全文索引ＡＢＣ１２３한글あいう三百二十个三百二十个全文索引南京市한글'),
(68, ' Tokenizer全文索引数据库南京市数据库123.45kg\nあいう三百二十个123.45kg  数据库English123.45kg，全文索引123.45kg全文索引\nＡＢＣ１２３EnglishＡＢＣ１２３三百二十个EnglishＡＢＣ１２３Tokenizer😀南京市Tokenizer 三百二十个😀Tokenizer한글Tokenizer Tokenizer 😀Tokenizer\t南京市😀 Tokenizer，南京市三百二十个ＡＢＣ１２３English三百二十个三百二十个한글English 长江大桥EnglishＡＢＣ１２３全文索引，한글，123.45kg南京市 \t한글123.45kg'),
(69, '数据库ＡＢＣ１２３あいうＡＢＣ１２３Tokenizer123.45kg'),
(70, '三百二十个三百二十个ＡＢＣ１２３，English한글123.45kgあいう123.45kg\tＡＢＣ１２３全文索引Tokenizer三百二十个😀\n한글南京市三百二十个Tokenizer\n123.45kg한글 三百二十个三百二十个あいう，长江大桥\n南京市Tokenizer长江大桥\n\tTokenizer\n\t ＡＢＣ１２３全文索引，Tokenizer数据库English三百二十个\t\n长江大桥数据库长江大桥한글한글한글 三百二十个\t English数据库123.45kg三百二十个，あいうEnglish😀Tokenizer长江大桥\t한글，数据库，Tokenizer长江大桥Tokenizer\n\n，Tokenizer \n长江大桥 한글\n'),
(71, '한글全文索引English한글123.45kgあいう南京市123.45kg\n南京市长江大桥三百二十个 数据库😀 三百二十个 ，\t长江大桥三百二十个한글全文索引ＡＢＣ１２３123.45kg\tEnglishＡＢＣ１２３English한글😀 全文索引한글\n EnglishTokenizer三百二十个한글あいう三百二十个\tあいう数据库南京市😀三百二十个😀南京市ＡＢＣ１２３南京市'),
(72, ' 南京市三百二十个ＡＢＣ１２３三百二十个\n😀长江大桥ＡＢＣ１２３三百二十个南京市English😀，\n\tＡＢＣ１２３123.45kg数据库 长江大桥😀全文索引，数据库 Tokenizer全文索引123.45kgＡＢＣ１２３数据库English三百二十个\t全文索引南京市，南京市长江大桥 \n全文索引123.45kg\n全文索引\n，한글한글Tokenizer数据库Tokenizer\n长江大桥 あいう');
SELECT 'ik:smart';
SELECT id, TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"smart"}]},{"output":"all"}]') FROM docs WHERE id NOT IN (10,11,12) ORDER BY id;
SELECT 'ik:max_word';
SELECT id, TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]},{"output":"all"}]') FROM docs WHERE id NOT IN (10,11,12) ORDER BY id;
SELECT 'beng:all';
SELECT id, TOKENIZE(CAST(body AS CHAR), 'beng', '[{"output":"all"}]') FROM docs WHERE id NOT IN (10,11,12) ORDER BY id;
-- Explicitly verify the long inputs in both IK modes.
SELECT 'long-ik-smart-10';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"smart"}]},{"output":"all"}]') FROM docs WHERE id=10;
SELECT 'long-ik-smart-11';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"smart"}]},{"output":"all"}]') FROM docs WHERE id=11;
SELECT 'long-ik-smart-12';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"smart"}]},{"output":"all"}]') FROM docs WHERE id=12;
SELECT 'long-ik-max_word-10';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]},{"output":"all"}]') FROM docs WHERE id=10;
SELECT 'long-ik-max_word-11';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]},{"output":"all"}]') FROM docs WHERE id=11;
SELECT 'long-ik-max_word-12';
SELECT TOKENIZE(CAST(body AS CHAR), 'ik', '[{"additional_args":[{"ik_mode":"max_word"}]},{"output":"all"}]') FROM docs WHERE id=12;
SELECT 'long-beng';
SELECT id, TOKENIZE(CAST(body AS CHAR), 'beng', '[{"output":"all"}]') FROM docs WHERE id IN (10,11,12) ORDER BY id;
DELETE FROM docs WHERE id IN (10,11,12);
CREATE FULLTEXT INDEX ft_ik ON docs(body) WITH PARSER ik;
SELECT id FROM docs WHERE MATCH(body) AGAINST('数据库 全文索引') ORDER BY id;
SELECT id, ROUND(MATCH(body) AGAINST('数据库 全文索引'), 9) FROM docs WHERE MATCH(body) AGAINST('数据库 全文索引') ORDER BY id;
DROP INDEX ft_ik ON docs;
CREATE FULLTEXT INDEX ft_beng ON docs(body) WITH PARSER beng;
SELECT id FROM docs WHERE MATCH(body) AGAINST('English tokenizer') ORDER BY id;
SELECT 'shared-casedown-parsers';
SELECT TOKENIZE('ＡＢＣ Mixed 中文 Iİ École １２３', 'space', '[{"output":"all"}]');
SELECT TOKENIZE('ＡＢＣ Mixed 中文 Iİ École １２３', 'ngram', '[{"output":"all"}]');
SELECT TOKENIZE('ＡＢＣ Mixed 中文 Iİ École １２３', 'ngram2', '[{"output":"all"}]');
DROP DATABASE codex_fts_pipeline;
